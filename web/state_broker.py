"""状态通信抽象层 — 模板 2/6. P88: 移除 Redis 依赖，纯内存实现。

接口:
  broker.get()       → dict   # 获取当前状态 (trades.db 为真相源)
  broker.update(d)   → None   # pipeline 推送进度/信号 + 广播 SSE
  broker.subscribe() → Queue  # SSE 客户端订阅
  broker.unsubscribe(q)       # SSE 客户端取消
"""
import json as _json
import threading, queue
import os as _os
import logging

_FINANCIAL_KEYS = ("capital", "total_asset", "pnl", "metrics", "pos_value", "positions")


class InProcessBroker:
    """纯内存实现 — pipeline 通过 HTTP POST 跨进程，SSE 通过内存 queue 推送。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._cache: dict = {}          # pipeline 进度/信号 (非财务数据)
        self._quote_ts = 0.0
        self._quote_result = None

    # ═══════════════════════════════════════════
    # 内部
    # ═══════════════════════════════════════════

    def _init_state(self) -> dict:
        """从 trades.db 构建完整财务状态 (唯一真相源)。"""
        import sys as _sys
        _root = _os.path.dirname(_os.path.dirname(__file__))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        state = {'progress': '',
                 'mood': {}, 'signals': [], 'sectors': [],
                 'summary': {}, 'timestamp': '', 'trace_id': ''}
        try:
            from quant.data.trade_repo import TradeRepo
            db = _os.path.join(_root, "quant", "data", "trades.db")
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
                market_db = _os.path.join(_root, "quant", "data", "market.db")
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
                    "shares": p["shares"], "price": p.get("price", 0),
                    "board_count": p.get("board_count", 0),
                    "buy_time": p.get("buy_time", ""),
                    "current": close_px,
                    "pnl_pct": round((close_px / p.get("price", 1) - 1) * 100, 2),
                    "value": round(p["shares"] * close_px, 2)
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
                "unrealized": round(total_pnl - realized, 2),
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
                market_db = _os.path.join(_root, "quant", "data", "market.db")
                if _os.path.exists(market_db):
                    mc = _sql3.connect(market_db)
                    syms = [p["symbol"] for p in positions]
                    if syms:
                        placeholders = ",".join(["?"] * len(syms))
                        rows = mc.execute(
                            f"SELECT symbol, name FROM stocks WHERE symbol IN ({placeholders})",
                            syms
                        ).fetchall()
                        name_map = {r[0]: r[1] for r in rows}
                        for p in positions:
                            if name_map.get(p["symbol"]):
                                p["name"] = name_map[p["symbol"]]
                    mc.close()
            except Exception:
                logging.getLogger("web.state_broker").warning("_init_state: stock close prices query failed", exc_info=True)

            # ── signals: 从 daily_signals 表读取 (cron 进程写入的唯一真相源) ──
            import json as _json_sig
            try:
                from datetime import datetime as _dt_sig
                today = _dt_sig.now().strftime("%Y-%m-%d")
                sig_path = _os.path.join(_root, "quant", "data", "trades.db")
                sc_sig = _sql2.connect(sig_path)
                sc_sig.row_factory = _sql2.Row
                # mode='live' 是实盘, 排除了回测写入的 backtest 信号
                sig_row = sc_sig.execute(
                    "SELECT signals_json FROM daily_signals WHERE date=? AND mode='live' "
                    "ORDER BY generated_at DESC LIMIT 1",
                    (today,)
                ).fetchone()
                if sig_row and sig_row["signals_json"]:
                    state["signals"] = _json_sig.loads(sig_row["signals_json"])
                sc_sig.close()
            except Exception:
                pass

            state["positions"] = positions
        except Exception:
            import logging
            logging.getLogger("web.state_broker").warning("_init_state failed", exc_info=True)
        return state

    def _quote_overlay(self, state: dict):
        """盘中实时报价覆盖持仓市值/总资产/PnL。"""
        import time as _time
        try:
            from quant.execution.quote import fetch_quotes
            from quant.execution.calendar import is_market_open
            if is_market_open() and state.get("positions"):
                now = _time.time()
                if self._quote_result is None or now - self._quote_ts > 5:
                    syms = [p["symbol"] for p in state["positions"]]
                    self._quote_result = fetch_quotes(syms)
                    self._quote_ts = now
                quotes = self._quote_result or {}
                if quotes:
                    new_pos_value = 0.0
                    for p in state["positions"]:
                        sym = p["symbol"]
                        q = quotes.get(sym, {})
                        if q and q.get("price", 0) > 0:
                            cur = q["price"]
                            p["current"] = cur
                            p["pnl_pct"] = round((cur / p["price"] - 1) * 100, 2) if p.get("price", 0) > 0 else 0
                            p["value"] = round(p["shares"] * cur, 2)
                        new_pos_value += p["value"]
                    state["pos_value"] = round(new_pos_value, 2)
                    cap = state.get("capital", 0)
                    state["total_asset"] = round(cap + new_pos_value, 2)
                    base = state.get("metrics", {}).get("initial_capital")
                    if base:
                        new_total_pnl = round(cap + new_pos_value - base, 2)
                        if state.get("pnl"):
                            state["pnl"]["total"] = new_total_pnl
                            state["pnl"]["unrealized"] = round(new_total_pnl - state["pnl"].get("realized", 0), 2)
                        if state.get("metrics"):
                            state["metrics"]["total_return_pct"] = round(new_total_pnl / base * 100, 2) if base > 0 else 0
        except Exception:
            logging.getLogger("web.state_broker").warning("_quote_overlay: position value calc failed", exc_info=True)

    # ═══════════════════════════════════════════
    # 公开接口
    # ═══════════════════════════════════════════

    def get(self) -> dict:
        """获取完整状态: trades.db 财务数据 + pipeline 进度/信号 overlay。"""
        state = self._init_state()
        with self._lock:
            cached = dict(self._cache)
        # pipeline 进度/信号 overlay (signals/progress/mood/trace_id/timestamp)
        for k in ("signals", "progress", "mood", "trace_id", "timestamp"):
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
        """接收 pipeline 推送的进度/信号，写入内存缓存并广播 SSE。"""
        data = {k: v for k, v in data.items() if k not in _FINANCIAL_KEYS}
        with self._lock:
            self._cache.update(data)
            payload = dict(self._cache)
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
