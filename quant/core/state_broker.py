"""状态通信抽象层 — 模板 2/6. 纯内存 + 文件桥实现跨进程。

v408: update() 写 JSON 文件桥 (pipeline→Web 跨进程)。
get() 从文件读 pipeline 进度/信号, 财务数据仍从 trades.db 读取。

接口:
  broker.get()       → dict   # 获取完整状态
  broker.update(d)   → None   # pipeline 推送进度/信号 + 写文件桥
  broker.subscribe() → Queue  # SSE 客户端订阅
  broker.unsubscribe(q)       # SSE 客户端取消
"""
import json as _json
import threading as _threading, queue
import os as _os
import logging
import sqlite3 as _sqlite
import time as _time
import copy as _copy
from quant.config.paths import MARKET_DB, TRADE_DB

_FINANCIAL_KEYS = ("capital", "total_asset", "pnl", "metrics", "pos_value", "positions")
_STATE_TTL = 3.0  # B-27 fix: 财务状态缓存 TTL (秒)

# v418 (R5): JSON 文件桥 (/tmp/quant_state_bridge.json) → SQLite 消息表。
# 跨进程 (pipeline cron → web) 状态传递, 单行整体存 JSON, 原子写。
_BRIDGE_DB = TRADE_DB
_BRIDGE_TABLE = "state_bridge"


class InProcessBroker:
    """纯内存实现 — pipeline 通过 HTTP POST 跨进程，SSE 通过内存 queue 推送。"""

    def __init__(self):
        self._lock = _threading.Lock()
        self._clients: list[queue.Queue] = []
        self._cache: dict = {}          # pipeline 进度/信号 (非财务数据)
        self._quote_ts = 0.0
        self._quote_result = None
        self._state_cache: dict = {}    # B-27 fix: _init_state 结果 TTL 缓存
        self._state_ts = 0.0
        self._init_state()
        self._start_quote_thread()

    # ═══════════════════════════════════════════
    # 内部
    # ═══════════════════════════════════════════

    def _init_state(self) -> dict:
        """从 trades.db 构建完整财务状态 (唯一真相源)。"""
        import sys as _sys
        _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(__file__)))  # v411: moved to quant/core
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        state = {'progress': '',
                 'mood': {}, 'signals': [], 'sectors': [],
                 'summary': {}, 'timestamp': '', 'trace_id': ''}
        try:
            from quant.data.repos import TradeRepo
            db = TRADE_DB
            repo = TradeRepo(db)
            # 首次启动自动播种策略资金
            if repo.get_initial_capital("quant") <= 0:
                from quant.config.constants import _require_cfg
                seed = float(_require_cfg("live.default_capital"))
                repo.set_initial_capital("quant", seed)
            capital = repo.get_cash("quant")
            raw_positions = repo.get_positions("quant")
            positions = []
            close_map = {}
            import sqlite3 as _sql2
            try:
                market_db = MARKET_DB
                if _os.path.exists(market_db):
                    mc = _sql2.connect(market_db)
                    for rp in raw_positions:
                        cr = mc.execute(
                            "SELECT close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1",
                            (rp["symbol"],)
                        ).fetchone()
                        if cr and cr[0]:
                            close_map[rp["symbol"]] = cr[0]
                    mc.close()
            except Exception:
                logging.getLogger("web.state_broker").warning("_init_state: stock close prices query failed", exc_info=True)

            for p in raw_positions:
                sym = p["symbol"]
                close_px = close_map.get(sym, p.get("price", 0))
                positions.append({
                    "symbol": sym, "name": "",
                    "strategy": "quant",  # test-v307: api_positions 过滤需要的字段
                    "shares": p["shares"], "price": p.get("price", 0),
                    "board_count": p.get("board_count", 0),
                    "buy_time": p.get("buy_time", ""),
                    "current": close_px,
                    "pnl_pct": round((close_px / p.get("price", 1) - 1) * 100, 2),
                    "value": round(p["shares"] * close_px, 2),
                    "industry": None, "sector": None,
                })
            pos_value = sum(p["value"] for p in positions)
            state["capital"] = round(capital, 2)
            state["total_asset"] = round(capital + pos_value, 2)
            state["pos_value"] = round(pos_value, 2)

            base = repo.get_initial_capital("quant")
            realized = repo.get_pnl("quant")
            total_pnl = round(capital + pos_value - base, 2)
            state["pnl"] = {
                "realized": round(realized, 2),
                "total": total_pnl,
                "unrealized": round(total_pnl - realized, 2) if pos_value > 0 else 0,
            }
            total_return_pct = round(total_pnl / base * 100, 2) if base > 0 else 0
            _, sells, wins = repo.get_counts("quant")
            win_rate = round(wins / sells * 100, 1) if sells > 0 else 0
            state["metrics"] = {
                "total_return_pct": total_return_pct,
                "win_rate": win_rate,
                "total_buys": repo.get_counts("quant")[0],
                "total_sells": sells,
                "initial_capital": base,
            }

            import sqlite3 as _sql3
            try:
                market_db = MARKET_DB
                if _os.path.exists(market_db):
                    mc = _sql3.connect(market_db)
                    syms = [p["symbol"] for p in positions]
                    if syms:
                        placeholders = ",".join(["?"] * len(syms))
                        rows = mc.execute(
                            f"SELECT symbol, name, industry FROM stocks WHERE symbol IN ({placeholders})",
                            syms
                        ).fetchall()
                        name_map = {r[0]: r[1] for r in rows}
                        ind_map = {r[0]: r[2] for r in rows if r[2]}
                        for p in positions:
                            if name_map.get(p["symbol"]):
                                p["name"] = name_map[p["symbol"]]
                            if ind_map.get(p["symbol"]):
                                p["industry"] = ind_map[p["symbol"]]
                                p["sector"] = ind_map[p["symbol"]]
                    mc.close()
            except Exception:
                logging.getLogger("web.state_broker").warning("_init_state: stock close prices query failed", exc_info=True)

            # ── signals: 从 daily_signals 表读取 (cron 进程写入的唯一真相源) ──
            import json as _json_sig
            try:
                from datetime import datetime as _dt_sig
                today = _dt_sig.now().strftime("%Y-%m-%d")
                sig_path = TRADE_DB
                sc_sig = _sql2.connect(sig_path)
                sc_sig.row_factory = _sql2.Row
                # mode='live' 是实盘, 排除了回测写入的 backtest 信号
                sig_row = sc_sig.execute(
                    "SELECT signals_json FROM daily_signals WHERE date=? AND mode='live' "
                    "ORDER BY generated_at DESC LIMIT 1",
                    (today,)
                ).fetchone()
                if sig_row and sig_row["signals_json"]:
                    signals = _json_sig.loads(sig_row["signals_json"])
                    # exec_notes: monitor 回写的执行状态 (test-v210)
                    exec_notes_str = sig_row.get("exec_notes") if hasattr(sig_row, "get") else None
                    if not exec_notes_str:
                        en_row = sc_sig.execute(
                            "SELECT exec_notes FROM daily_signals WHERE date=? AND mode='live' ORDER BY generated_at DESC LIMIT 1",
                            (today,)
                        ).fetchone()
                        exec_notes_str = en_row["exec_notes"] if en_row else "{}"
                    try:
                        exec_notes = _json_sig.loads(exec_notes_str) if exec_notes_str else {}
                    except Exception as _en_err:
                        exec_notes = {}
                        # v418 (R2): 原静默降级 → warning 可观测
                        logging.getLogger("web.state_broker").warning(
                            f"_init_state: exec_notes JSON 解析失败 (降级空dict): {_en_err}")
                    for s in signals:
                        s["exec_note"] = exec_notes.get(s.get("symbol", ""), "")
                    # 从 market.db stocks 表补充名称 (test-v205)
                    try:
                        mdb = _sql2.connect(_os.path.join(_root, "quant", "data", "market.db"))
                        symbols = [s["symbol"] for s in signals if "symbol" in s]
                        if symbols:
                            placeholders = ",".join(["?"] * len(symbols))
                            name_map = dict(mdb.execute(
                                f"SELECT symbol, name FROM stocks WHERE symbol IN ({placeholders})",
                                symbols
                            ).fetchall())
                            for s in signals:
                                s["name"] = name_map.get(s.get("symbol", ""), "")
                        mdb.close()
                    except Exception as _nm_err:
                        # v418 (R2): 原静默 → warning 可观测
                        logging.getLogger("web.state_broker").warning(
                            f"_init_state: 信号股票名称补充失败: {_nm_err}")
                    state["signals"] = signals
                sc_sig.close()
            except Exception as _sig_err:
                # v418 (R2): 原静默吞错 → warning + exc_info (signals 是监控页核心数据)
                logging.getLogger("web.state_broker").warning(
                    f"_init_state: 读取 daily_signals 失败: {_sig_err}", exc_info=True)

            state["positions"] = positions
            # test-v310: 市场状态实时展示 (非仅信号生成时)
            try:
                from quant.regime.detector import get_current_regime
                from quant.optimizer.portfolio import _get_regime_max_lots
                rlabel, rprobs = get_current_regime()
                if rlabel:
                    state["regime"] = rlabel
                    # v418 (R8): 删 get_regime_sizing (capital 乘数法已废弃) —
                    # 展示 lot-based 手数上限 (web UI 只读展示)
                    state["regime_max_lots"] = _get_regime_max_lots("micro", rlabel)
                    state["regime_confidence"] = round(rprobs.get(rlabel, 0), 2)
            except Exception as _reg_err:
                # v418 (R2): 原静默 → warning (regime 缺失降级展示, 不阻断)
                logging.getLogger("web.state_broker").warning(
                    f"_init_state: regime 状态不可用: {_reg_err}")
        except Exception:
            import logging
            logging.getLogger("web.state_broker").warning("_init_state failed", exc_info=True)
        return state

    def _start_quote_thread(self):
        """后台线程: 每 3s 刷新实时报价到 _quote_result (唯一的 fetch_quotes 调用点)。"""
        _quotes_errors = 0

        def _refresh_loop():
            nonlocal _quotes_errors
            import time as _t
            while True:
                try:
                    from quant.data.repos import TradeRepo
                    from quant.execution.quote import fetch_quotes
                    from quant.execution.calendar import is_market_open
                    if is_market_open():
                        raw = TradeRepo().get_positions("quant")
                        if raw:
                            syms = [p["symbol"] for p in raw]
                            self._quote_result = fetch_quotes(syms) or {}
                            self._quote_ts = _t.time()
                        _quotes_errors = 0
                except Exception as _q_err:
                    # v418 (R2): 原静默 → 连续失败才告警 (每 3s 轮询, 单次失败不刷屏)
                    _quotes_errors += 1
                    if _quotes_errors in (1, 10, 100, 1000):
                        logging.getLogger("web.state_broker").warning(
                            f"quote refresh failed ({_quotes_errors} consecutive): {_q_err}")
                _t.sleep(3)
        t = _threading.Thread(target=_refresh_loop, daemon=True, name="quote-refresh")
        t.start()

    def _quote_overlay(self, state: dict):
        """用 _quote_result 缓存覆盖持仓现价/市值/PnL (不独立拉行情)。"""
        try:
            quotes = self._quote_result or {}
            positions = state.get("positions", [])
            if quotes and positions:
                new_pos_value = 0.0
                for p in positions:
                    q = quotes.get(p["symbol"], {})
                    if q and q.get("price", 0) > 0:
                        cur = q["price"]
                        p["current"] = cur
                        p["pnl_pct"] = round((cur / p["price"] - 1) * 100, 2) if p.get("price", 0) > 0 else 0
                        p["value"] = round(p["shares"] * cur, 2)
                    new_pos_value += p.get("value", 0)
                state["pos_value"] = round(new_pos_value, 2)
                cap = state.get("capital", 0)
                state["total_asset"] = round(cap + new_pos_value, 2)
                base = state.get("metrics", {}).get("initial_capital")
                if base:
                    new_total_pnl = round(cap + new_pos_value - base, 2)
                    if state.get("pnl"):
                        state["pnl"]["total"] = new_total_pnl
                        state["pnl"]["unrealized"] = round(new_total_pnl - state["pnl"].get("realized", 0), 2) if new_pos_value > 0 else 0
                    if state.get("metrics"):
                        state["metrics"]["total_return_pct"] = round(new_total_pnl / base * 100, 2) if base > 0 else 0
        except Exception:
            logging.getLogger("web.state_broker").warning("_quote_overlay failed", exc_info=True)

    # ═══════════════════════════════════════════
    # 公开接口
    # ═══════════════════════════════════════════

    def get(self) -> dict:
        """获取完整状态: trades.db 财务数据 + pipeline 进度/信号 overlay。"""
        # B-27 fix: 财务状态做 TTL 缓存 (原每次 get() 全量重建 → 4 类 DB 查询/次,
        # SSE 轮询下 DB 压力线性放大); overlay (quotes/status) 仍实时计算
        now = _time.monotonic()
        with self._lock:
            if self._state_cache and (now - self._state_ts) < _STATE_TTL:
                # 深拷贝: _quote_overlay 会原地改写 pnl/metrics/positions,
                # 浅拷贝会把缓存污染
                state = _copy.deepcopy(self._state_cache)
            else:
                state = None
        if state is None:
            state = self._init_state()
            with self._lock:
                self._state_cache = _copy.deepcopy(state)
                self._state_ts = _time.monotonic()
        with self._lock:
            cached = dict(self._cache)
        # v418 (R5): 从 SQLite 消息表读取 pipeline 进度 (跨进程可见)
        # 财务数据仍从 DB 读取, 只 overlay progress/signals/trace_id/timestamp
        try:
            _conn = _sqlite.connect(_BRIDGE_DB, timeout=2.0)
            _conn.row_factory = _sqlite.Row
            _conn.execute(
                f"CREATE TABLE IF NOT EXISTS {_BRIDGE_TABLE} "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            _row = _conn.execute(
                f"SELECT payload FROM {_BRIDGE_TABLE} WHERE id = 1"
            ).fetchone()
            _conn.close()
            if _row and _row["payload"]:
                _bridge_data = _json.loads(_row["payload"])
                # v513: alerts 并入桥接 (行业同步进程→web SSE 跨进程告警)
                for k in ("signals", "progress", "mood", "trace_id", "timestamp", "alerts"):
                    if k in _bridge_data:
                        cached[k] = _bridge_data[k]
        except Exception as _br_err:
            # v418 (R5): 原静默 pass → warning 可观测 (桥仅进度显示, 不阻断)
            logging.getLogger("web.state_broker").warning(
                f"get(): state_bridge 读取失败: {_br_err}")
        # pipeline 进度/信号 overlay (signals/progress/mood/trace_id/timestamp)
        # v513: + alerts (跨进程告警 — 行业同步每日上限等)
        for k in ("signals", "progress", "mood", "trace_id", "timestamp", "alerts"):
            if k in cached:
                state[k] = cached[k]
        # Dynamically inject trading period status
        try:
            from quant.execution.calendar import get_trading_period
            state['status'] = get_trading_period()
        except Exception:
            state['status'] = 'unknown'
        self._quote_overlay(state)
        return state

    def update(self, data: dict):
        """接收 pipeline 推送的进度/信号, 写内存缓存 + JSON 文件桥 (v408 跨进程)."""
        data = {k: v for k, v in data.items() if k not in _FINANCIAL_KEYS}
        with self._lock:
            self._cache.update(data)
            payload = dict(self._cache)
        # v418 (R5): 写 SQLite 消息表 — pipeline 和 web 是不同进程,
        # 纯内存 _cache 跨进程不可见. 原子 upsert 单行 JSON payload.
        try:
            _conn = _sqlite.connect(_BRIDGE_DB, timeout=2.0)
            _conn.execute(
                f"CREATE TABLE IF NOT EXISTS {_BRIDGE_TABLE} "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            _conn.execute(
                f"INSERT OR REPLACE INTO {_BRIDGE_TABLE} (id, payload, updated_at) VALUES (1, ?, ?)",
                (_json.dumps(payload, ensure_ascii=False), _time.time()),
            )
            _conn.commit()
            _conn.close()
        except Exception as _br_err:
            # v418 (R5): 原静默 pass → warning 可观测 (桥仅进度显示, 不阻断 pipeline)
            logging.getLogger("web.state_broker").warning(
                f"update(): state_bridge 写入失败: {_br_err}")
        dead = []
        for q in self._clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            self._clients.remove(q)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=10)
        self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        try:
            self._clients.remove(q)
        except ValueError:
            pass


# 全局单例
broker = InProcessBroker()
