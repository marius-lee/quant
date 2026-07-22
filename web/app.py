"""量化选股 Web — 7 层架构监控仪表盘。

状态: web/shared.py 内存共享 (pipeline 写入, Flask 读取)
持久: quant/data/trades.db (交易唯一真相源)
"""

import json, os, sqlite3
from quant.config.constants import _require_cfg

from quant.utils.excepthook import setup; setup()
from quant.config.paths import TRADE_DB, MARKET_DB  # crash → app.log
from quant.config.loader import get as cfg, validate; validate()  # 启动时校验 config.yaml 类型
from quant.data.store import market_conn  # P69: 统一连接层
from datetime import date, datetime
from flask import Flask, jsonify, render_template

# 前端版本标识 — 修改此处触发浏览器刷新认知
VERSION = "test-v216"
# ── 进程退出埋点 ──
import atexit as _atexit, signal as _signal, sys as _sys, threading as _thr, os as _os
def _log_exit(reason: str = ""):
    try:
        from quant.utils.logger import get_logger
        get_logger("web.app").warning(
            f"EXIT | reason={reason or 'unknown'} | pid={os.getpid()} | "
            f"thread={_thr.current_thread().name}")
    except Exception:
        print(f"[EXIT] {reason} pid={os.getpid()}", flush=True)

def _clean_exit(reason: str):
    """P78: ThreadPoolExecutor 线程随 with 语句自动回收, 无需手动清理."""
    _log_exit(reason)
    _sys.exit(0)

_atexit.register(_log_exit, "atexit")
_signal.signal(_signal.SIGTERM, lambda s, f: _clean_exit("SIGTERM"))
_signal.signal(_signal.SIGINT,  lambda s, f: _clean_exit("SIGINT"))

from quant.utils.logger import get_logger

logger = get_logger("web.app")

app = Flask(__name__)
_DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"


def _api_response(data=None, *, meta=None, error=None):
    """模板 6: 统一 API 响应信封 {data, meta, error}.
    error 格式: {"code": "ERROR_CODE", "message": "人类可读描述", "details": [...]} (可选)
    """
    return jsonify({"data": data, "meta": meta, "error": error})
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# 启动时异步预热因子评估缓存 (首次 /api/factors 请求免等待)
import threading
# 缓存预热已移除 — web 启动不应触发因子计算, 首次 API 请求时懒加载


def _capital(strategy: str) -> float:
    """从 strategy_config 表读本金。无记录时默认 5000 并自动写入。"""
    from quant.data.trade_repo import TradeRepo
    repo = TradeRepo()
    cap = repo.get_initial_capital(strategy)
    if cap <= 0:
        from quant.config.constants import _require_cfg as _rcf
        cap = float(_rcf("live.default_capital"))
        repo.set_initial_capital(strategy, cap)
    return cap

from web.state_broker import broker
from web.shared import get_state, update_state  # deprecated, kept for compat


# ═══════════════════════════════════════════════════════════
# 核心 API
# ═══════════════════════════════════════════════════════════

@app.route("/")
def index():
    """首页 — 传递 perf 数据供服务端渲染仪表盘."""
    try:
        from quant.data.trade_repo import TradeRepo
        repo = TradeRepo()
        base = repo.get_initial_capital("quant")
        capital = repo.get_cash("quant") or base
        position_cost = repo.get_open_position_cost("quant")  # (2026-07-21 audit M3)
        # 优先用最新收盘价估值, 与绩效页面口径一致 (test-v201)
        position_value = position_cost
        try:
            mc = sqlite3.connect(MARKET_DB)
            pos_rows = sqlite3.connect(TRADE_DB).execute(
                "SELECT symbol, SUM(shares) FROM sim_trades WHERE side='buy' AND strategy='quant' AND mode='live'"
                " AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy='quant' AND mode='live')"
                " GROUP BY symbol"
            ).fetchall()
            if pos_rows:
                close_mv = 0.0
                for sym, shares in pos_rows:
                    cr = mc.execute(
                        "SELECT close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)
                    ).fetchone()
                    if cr and cr[0] and cr[0] > 0:
                        close_mv += cr[0] * shares
                if close_mv > 0:
                    position_value = round(close_mv, 2)
            mc.close()
        except Exception:
            pass  # fall through to position_cost
        total_asset = round(capital + position_value, 2)
        total_pnl = round(total_asset - base, 2)
        perf = {"total_pnl": total_pnl, "total_asset": total_asset, "initial_capital": base}
    except Exception:
        perf = {"total_pnl": 0, "total_asset": 5000, "initial_capital": 5000}
    return render_template("index.html", version=VERSION, perf=perf)


@app.route("/api/state")
def api_state():
    """当前完整状态 (模板6: {data, error} 信封): 资金 + 持仓 + 信号 + 暴露"""
    return _api_response(data=get_state())


@app.route("/api/factors")
def api_factors():
    """因子评估数据 — 为前端因子分析 Tab 提供 IC/IR/衰减/相关性。

    数据来源: factor_snapshot 表 (24h 过期自动重算)
    首次访问时自动计算 (约 30s), 后续 24h 内秒出。
    ?refresh=true 强制重新计算。
    """
    from flask import request
    from quant.factor.stats_cache import get_cached_factor_stats
    force = request.args.get("refresh", "false").lower() == "true"
    try:
        stats = get_cached_factor_stats(force_refresh=force)
        # 补充 factor_registry 总/有效计数
        try:
            from quant.data.repos import FactorRepo
            repo = FactorRepo()
            dist = repo.status_distribution()
            stats["n_total"] = repo.count_total()
            stats["n_active"] = dist.get("active", 0)
            stats["n_candidate"] = dist.get("candidate", 0)
            stats["n_rejected"] = dist.get("rejected", 0)
            stats["n_retired"] = dist.get("retired", 0)
            stats["n_monitoring"] = dist.get("monitoring", 0)
            known = stats["n_active"] + stats["n_candidate"] + stats["n_rejected"] + stats["n_retired"] + stats["n_monitoring"]
            stats["n_registered"] = stats["n_total"] - known
            stats["n_evaluated"] = repo.count_with_ic()
            # Use the same variable name for the except handler
            c = None  # no longer needed
        except Exception:
            logger.warning("api_factors: factor_registry query failed", exc_info=True)
            stats["n_total"] = 0
            stats["n_registered"] = 0
            stats["n_active"] = 0
            stats["n_candidate"] = 0
            stats["n_rejected"] = 0
            stats["n_retired"] = 0
            stats["n_monitoring"] = 0
            stats["n_evaluated"] = 0
        return _api_response(data=stats)
    except Exception as e:
        from quant.utils.logger import get_logger
        get_logger("web.app").warning(f"Factor stats failed: {e}")
        return _api_response(error={"code": "FACTOR_ERROR", "message": str(e)})


@app.route("/api/positions")
def api_positions():
    """持仓 — broker 数据 (含 state_broker 实时现价 overlay)。"""
    from flask import request
    strategy = request.args.get("strategy", "quant")
    limit = min(int(request.args.get("limit", 100)), 100)
    offset = int(request.args.get("offset", 0))
    try:
        state = broker.get()
        positions = state.get("positions", [])
        if strategy and strategy != "quant":
            positions = [p for p in positions if p.get("strategy", "quant") == strategy]
        paged = positions[offset:offset + limit]
        return _api_response(data={"positions": paged}, meta={"total": len(positions), "limit": limit, "offset": offset})
    except Exception:
        logger.warning("api_positions failed", exc_info=True)
        return _api_response(error={"code": "INTERNAL", "message": "positions query failed"}), 500


@app.route("/api/trades")
def api_trades():
    """交易历史. ?strategy=过滤&limit=N&offset=M (模板6分页)"""
    from flask import request
    strategy = request.args.get("strategy", "quant")
    limit = min(int(request.args.get("limit", 100)), 100)
    offset = int(request.args.get("offset", 0))
    trades = []
    positions = []
    try:
        from quant.data.trade_repo import TradeRepo
        repo = TradeRepo(TRADE_DB)
        if strategy:
            raw_trades = repo.get_trades(strategy, limit=10000)  # 前端展示上限, 防止浏览器卡死, 非业务参数
            raw_positions = repo.get_positions(strategy)
        else:
            raw_trades = repo.get_trades(None, limit=10000)  # 前端展示上限, 防止浏览器卡死, 非业务参数
            raw_positions = repo.get_positions(None)
        # enrich with stock names from market.db
        _names = {}
        try:
            import sqlite3, os
            _mdb = os.path.join(os.path.dirname(__file__), "..", "quant", "data", "market.db")
            _mc = sqlite3.connect(_mdb)
            _mc.execute("PRAGMA busy_timeout=3000")
            _syms = list(set(t["symbol"] for t in (raw_trades or [])))
            if _syms:
                _ph = ",".join("?" * len(_syms))
                _names = {r[0]: r[1] for r in _mc.execute(
                    f"SELECT symbol, name FROM stocks WHERE symbol IN ({_ph})", _syms
                ).fetchall()}
            _mc.close()
        except Exception as _e:
            logger.warning(f"api_trades: stock name lookup failed (non-fatal): {_e}")

        trades = [{"date": (t.get("created_at") or t.get("date") or "")[:19],
                    "symbol": t["symbol"], "name": _names.get(t["symbol"], ""),
                    "side": t["side"], "price": t["price"],
                    "shares": t["shares"], "pnl": t.get("pnl") or 0, "pnl_pct": t.get("pnl_pct") or 0}
                   for t in (raw_trades or [])]
        positions = [{"symbol": p["symbol"], "price": p.get("price", 0),
                       "shares": p["shares"], "board_count": p.get("board_count", 0),
                       "date": p.get("buy_time", "")} for p in (raw_positions or [])]
        # Clean up any old import
    except Exception:
        logger.warning("api_trades: query failed (schema mismatch?)", exc_info=_DEBUG)
        return _api_response(error={"code": "INTERNAL", "message": "trades query failed"}), 500
    return _api_response(data={"trades": trades[offset:offset + limit], "positions": positions}, meta={"total_trades": len(trades), "limit": limit, "offset": offset})


@app.route("/api/trade", methods=["POST"])
def api_record_trade():
    """记录一笔交易 → trades.db (手动交易，strategy='manual')"""
    from flask import request
    data = request.get_json(force=True)
    side = data.get("side")
    strategy = "manual"
    symbol = data.get("symbol")
    try:
        price = float(data.get("price", 0))
        shares = int(data.get("shares", 0))
        cost = float(data.get("cost", 0))
    except (TypeError, ValueError):
        return _api_response(error={"code": "INVALID_PARAMETER", "message": "price/shares/cost 格式错误", "field": "price/shares/cost"}), 400

    # 模板 1: 输入边界校验
    if not symbol or not isinstance(symbol, str) or len(symbol) != 6:
        return _api_response(error={"code": "INVALID_PARAMETER", "message": "symbol 必须是6位代码", "field": "symbol"}), 400
    if price <= 0 or price > 100000:
        return _api_response(error={"code": "INVALID_PARAMETER", "message": "price 超出范围 (0, 100000]", "field": "price"}), 400
    if shares < 100 or shares % 100 != 0:
        return _api_response(error={"code": "INVALID_PARAMETER", "message": "shares 必须是100的整数倍且≥100", "field": "shares"}), 400
    if side not in ("buy", "sell"):
        return _api_response(error={"code": "INVALID_PARAMETER", "message": "side 必须是 buy 或 sell", "field": "side"}), 400

    today = date.today().isoformat()
    from quant.data.trade_repo import TradeRepo
    repo = TradeRepo()

    if side == "buy":
        repo.record_trade(strategy, today, symbol, "buy", price, shares,
                          board_count=data.get("board_count", 0))
        return _api_response(data={"ok": True})

    elif side == "sell":
        pnl = (price - cost) * shares
        pnl_pct = round((price / cost - 1) * 100, 2) if cost > 0 else 0
        repo.record_trade(strategy, today, symbol, "sell", price, shares,
                          pnl=round(pnl, 2), pnl_pct=pnl_pct)
        return _api_response(data={"ok": True, "pnl": round(pnl, 2), "pnl_pct": pnl_pct})

    else:
        return _api_response(error={"code": "INVALID_PARAMETER", "message": "side必须是buy或sell", "field": "side"}), 400


@app.route("/api/state", methods=["POST"])
def api_update_state():
    """pipeline 更新状态"""
    from flask import request
    data = request.get_json(force=True)
    data["timestamp"] = datetime.now().isoformat()
    update_state(data)
    return _api_response(data={"ok": True})


@app.route("/api/quotes")
def api_quotes():
    """实时行情 — 批量拉取新浪财经报价。

    ?symbols=000001,600036,430047 — 逗号分隔的股票代码列表
    返回: {quotes: {symbol: {price, change_pct, ...}}, status: "open"|"closed"}
    仅在交易日 9:30-15:00 拉取, 否则返回空。
    止盈止损已移到 monitor.py 盘中风控统一管理。
    """
    from flask import request
    from quant.execution.quote import fetch_quotes, is_trading_time
    syms_str = request.args.get("symbols", "")
    if not syms_str:
        return _api_response(data={"quotes": {}})
    symbols = [s.strip() for s in syms_str.split(",") if s.strip() and len(s.strip()) == 6]
    if not symbols:
        return _api_response(data={"quotes": {}})
    if not is_trading_time():
        return _api_response(data={"quotes": {}, "status": "closed"})
    quotes = fetch_quotes(symbols)

    return _api_response(data={
        "quotes": quotes, "status": "open",
    })



@app.route("/api/risk")
def api_risk():
    """风险暴露 — 持仓波动率 & 最大回撤 (60日滚动).

    ?symbols=002072,002767 — 需要计算的持仓代码列表
    返回: {symbols: [{symbol, weight_pct, annual_vol_pct, max_dd_pct}]}
    """
    from flask import request
    syms_str = request.args.get("symbols", "")
    symbols = [s.strip() for s in syms_str.split(",") if s.strip() and len(s.strip()) == 6]
    if not symbols:
        return _api_response(data={"symbols": []})

    import sqlite3, math
    market_db = MARKET_DB
    result = []
    try:
        mc = market_conn("ro")
        for sym in symbols:
            rows = mc.execute(
                "SELECT close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT ?",
                (sym, int(cfg("risk.rolling_window")))
            ).fetchall()
            if len(rows) < 10:
                result.append({"symbol": sym, "weight_pct": 0, "annual_vol_pct": 0,
                               "max_dd_pct": 0, "days": len(rows)})
                continue
            closes = [r[0] for r in reversed(rows)]
            # daily log returns
            logrets = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
            n = len(logrets)
            if n < 2:
                result.append({"symbol": sym, "weight_pct": 0, "annual_vol_pct": 0,
                               "max_dd_pct": 0, "days": len(rows)})
                continue
            mean_ret = sum(logrets) / n
            variance = sum((r - mean_ret) ** 2 for r in logrets) / (n - 1)
            annual_vol = math.sqrt(variance * 252) * 100  # annualized %
            # max drawdown
            peak = closes[0]
            max_dd = 0.0
            for c in closes:
                if c > peak:
                    peak = c
                dd = (peak - c) / peak
                if dd > max_dd:
                    max_dd = dd
            result.append({
                "symbol": sym,
                "annual_vol_pct": round(annual_vol, 1),
                "max_dd_pct": round(max_dd * 100, 1),
                "days": len(rows),
            })
    except Exception as e:
        logger.warning(f"risk query failed: {e}")
        return _api_response(error={"code": "INTERNAL", "message": str(e)}), 500

    # Merge with portfolio weights from state
    state = broker.get()
    positions = state.get("positions", [])
    pos_map = {p["symbol"]: p.get("value", 0) for p in positions}
    total_val = sum(pos_map.values())
    for r in result:
        r["weight_pct"] = round(pos_map.get(r["symbol"], 0) / total_val * 100, 1) if total_val > 0 else 0

    return _api_response(data={"symbols": result, "total_value": round(total_val, 2)})

@app.route("/api/performance")
def api_performance():
    """累计绩效统计. ?strategy=quant&quotes=true (quotes=true 用市价估值)"""
    from flask import request
    strategy = request.args.get("strategy", "quant")
    tc = sqlite3.connect(TRADE_DB)
    sells = tc.execute("SELECT pnl FROM sim_trades WHERE side='sell' AND strategy=?", (strategy,)).fetchall()
    realized_pnl = sum(r[0] for r in sells if r[0])
    win_trades = sum(1 for r in sells if r[0] and r[0] > 0)
    total_sells = len(sells)
    win_rate = round(win_trades / total_sells * 100, 1) if total_sells > 0 else 0
    buys = tc.execute("SELECT COUNT(*) FROM sim_trades WHERE side='buy' AND strategy=?", (strategy,)).fetchone()[0]
    from quant.data.trade_repo import TradeRepo; base = TradeRepo().get_initial_capital(strategy)
    capital = TradeRepo().get_cash(strategy) or base
    position_cost = tc.execute(
        "SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
        (strategy, strategy)).fetchone()[0]

    # 估值: ?quotes=true → 市价; 默认 → 最新收盘, 均失败则账面成本 (test-v203)
    use_quotes = request.args.get("quotes", "").lower() == "true"
    position_market_value = position_cost  # 默认用成本
    valuation_method = "book_cost"
    # shares_map 提前填充, latest_close fallback 不依赖 use_quotes
    shares_map = dict(tc.execute(
        "SELECT symbol, SUM(shares) FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?) GROUP BY symbol",
        (strategy, strategy)).fetchall())
    if use_quotes:
        try:
            pos_symbols = [r[0] for r in tc.execute(
                "SELECT symbol FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
                (strategy, strategy)).fetchall()]
            from quant.execution.quote import fetch_quotes
            quotes = fetch_quotes(pos_symbols)
            if quotes:
                pos_share_map = dict(tc.execute(
                    "SELECT symbol, SUM(shares) FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?) GROUP BY symbol",
                    (strategy, strategy)).fetchall())
                mv = 0.0
                for sym, shares in pos_share_map.items():
                    if sym in quotes:
                        mv += quotes[sym]["price"] * shares
                    elif not sym.startswith(("4","8","92")):
                        mv += position_cost / len(pos_share_map) if pos_share_map else 0
                if mv > 0:
                    position_market_value = round(mv, 2)
                    valuation_method = "market"
        except Exception:
            logger.warning("api_performance: market valuation failed", exc_info=True)

    # ── fallback to latest close (盘后/休市) ──
    if valuation_method == "book_cost" and position_cost > 0:
        try:
            mc = market_conn("ro")
            close_mv = 0.0
            for sym, shares in shares_map.items():
                cr = mc.execute(
                    "SELECT close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)
                ).fetchone()
                if cr and cr[0] and cr[0] > 0:
                    close_mv += cr[0] * shares_map[sym]
            if close_mv > 0:
                position_market_value = round(close_mv, 2)
                valuation_method = "latest_close"
        except Exception:
            logger.warning("api_performance: latest close valuation failed", exc_info=True)

    total_asset = round(capital + position_market_value, 2)
    total_pnl = round(total_asset - base, 2)
    tc.close()

    # 计算 Sharpe 和最大回撤: 从 daily_equity 表读权益序列 (test-v201)
    # capital_after 在 record_trade 从未写入, daily_equity 是唯一权益快照来源
    sharpe = 0.0
    max_drawdown = 0.0
    import pandas as _pd
    from quant.monitor.attribution import compute_sharpe, compute_max_drawdown

    _eqconn = sqlite3.connect(TRADE_DB)
    _eq_rows = _eqconn.execute(
        "SELECT total_equity FROM daily_equity ORDER BY date"
    ).fetchall()
    _eqconn.close()

    rets = []
    if len(_eq_rows) >= 3:
        _prev_eq = _eq_rows[0][0]
        for (_eq,) in _eq_rows[1:]:
            if _prev_eq > 0:
                rets.append(_eq / _prev_eq - 1)
            _prev_eq = _eq
    else:
        # fallback: daily_equity 不足时从卖出 PnL 构建近似权益曲线
        _sdconn = sqlite3.connect(TRADE_DB)
        _sd = _sdconn.execute(
            "SELECT date, pnl FROM sim_trades WHERE side='sell' AND strategy=? AND mode='live' ORDER BY date",
            (strategy,)
        ).fetchall()
        _sdconn.close()
        if _sd:
            _cum = base
            for _date, _p in _sd:
                _cum += (_p or 0)
                if _cum > 0:
                    rets.append((_p or 0) / _cum)

    if len(rets) >= 3:
        _sr = _pd.Series(rets)
        sharpe = compute_sharpe(_sr)
        max_drawdown = compute_max_drawdown(_sr)

    result = {
        "realized_pnl": round(realized_pnl, 2),
        "total_pnl": total_pnl,
        "total_asset": total_asset,
        "initial_capital": base,
        "total_return_pct": round(total_pnl / base * 100, 2) if base > 0 else 0,
        "total_sells": total_sells,
        "win_rate": win_rate,
        "total_buys": buys,
        "capital": round(capital, 2),
        "valuation_method": valuation_method,
        "sharpe": round(sharpe, 2),
        "max_drawdown": round(max_drawdown, 4),
    }
    return _api_response(data=result)


@app.route("/openapi.json")
def api_openapi():
    """OpenAPI 3.0 规范 (模板 6)"""
    return jsonify({
        "openapi": "3.0.3",
        "info": {"title": "quant API", "version": "1.0.0", "description": "A股量化选股系统 API"},
        "paths": {
            "/api/state": {
                "get": {"summary": "系统状态", "responses": {"200": {"description": "OK"}}},
                "post": {"summary": "更新系统状态", "responses": {"200": {"description": "OK"}}}
            },
            "/api/factors": {"get": {"summary": "因子评估数据", "parameters": [{"name": "refresh", "in": "query", "schema": {"type": "boolean"}}], "responses": {"200": {"description": "OK"}}}},
            "/api/positions": {"get": {"summary": "当前持仓", "parameters": [{"name": "strategy", "in": "query"}, {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 100}}, {"name": "offset", "in": "query", "schema": {"type": "integer"}}], "responses": {"200": {"description": "OK"}}}},
            "/api/trades": {"get": {"summary": "交易历史", "parameters": [{"name": "strategy", "in": "query"}, {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 100}}, {"name": "offset", "in": "query", "schema": {"type": "integer"}}], "responses": {"200": {"description": "OK"}}}},
            "/api/trade": {"post": {"summary": "记录交易", "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}, "responses": {"200": {"description": "OK"}, "400": {"description": "参数错误"}}}},
            "/api/stream": {"get": {"summary": "SSE 状态推送 (实时)", "responses": {"200": {"description": "text/event-stream"}}}}, "/api/quotes": {"get": {"summary": "实时行情", "parameters": [{"name": "symbols", "in": "query", "required": True}], "responses": {"200": {"description": "OK"}}}},
            "/api/performance": {"get": {"summary": "绩效统计", "parameters": [{"name": "strategy", "in": "query"}, {"name": "quotes", "in": "query", "schema": {"type": "boolean"}}], "responses": {"200": {"description": "OK"}}}},
        }
    })

@app.route("/api/stream")
def api_stream():
    """模板 6 + 方案B: SSE 实时推送状态变更 (替代轮询)."""
    import json, queue
    from flask import Response
    q = broker.subscribe()
    from quant.execution.calendar import get_trading_period as _sp
    def generate():
        try:
            # 先发当前状态
            init = broker.get()
            init["status"] = _sp()
            yield f"data: {json.dumps(init, ensure_ascii=False)}\n\n"
            while True:
                try:
                    data = q.get(timeout=_require_cfg("web.sse.queue_timeout"))
                    data["status"] = _sp(); yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            broker.unsubscribe(q)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/health")
def api_health():
    """模板9 T1: 健康检查 — DB连接 + 最近 pipeline 状态."""
    import sqlite3, os as _os, time as _time
    status = {"status": "ok", "checks": {}}
    # DB 连通性
    try:
        db = MARKET_DB
        conn = market_conn("ro")
        conn.execute("SELECT 1").fetchone()
        status["checks"]["market_db"] = "ok"
    except Exception as e:
        status["checks"]["market_db"] = f"fail: {e}"
        status["status"] = "degraded"
    # 最近 pipeline 状态
    state = broker.get()
    status["pipeline"] = {
        "last_progress": state.get("progress", ""),
        "last_trace_id": state.get("trace_id", ""),
    }
    from quant.monitor.metrics import metrics as _mm
    status["metrics"] = _mm.snapshot()
    # 告警检查
    from quant.monitor.alerts import check_alerts
    status["alerts"] = check_alerts(state, _mm.snapshot())
    return _api_response(data=status)

@app.route("/api/scheduler")
def api_scheduler():
    """调度器状态 — DB 驱动的任务监控 (不再从日志刮取)."""
    import json, os
    from datetime import datetime
    import sqlite3
    from quant.scheduler.status import register_all, all_tasks

    _proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cron_marker = os.path.join(_proj, ".cron_installed")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # ── 1. crontab 配置检测 ──
    # ── 从单一真相源 (status.register_all) 获取任务定义 ──
    register_all()
    cron_installed = os.path.exists(cron_marker)
    cron_tasks = {t["name"] for t in all_tasks()} if cron_installed else set()

    # ── 2. DB 查询 (统一入口: market.db → task_runs 表) ──
    from quant.config.paths import MARKET_DB
    db_runs = {}  # task_name → {status, finished_at, error, summary}
    try:
        conn = sqlite3.connect(MARKET_DB)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        # 取每个任务今天的最新一条记录
        rows = conn.execute(
            "SELECT task_name, status, started_at, finished_at, error, summary "
            "FROM task_runs WHERE date = ? ORDER BY id DESC",
            (today_str,)
        ).fetchall()
        for r in rows:
            key = r["task_name"]
            if key not in db_runs:  # 第一条是最新的
                db_runs[key] = {
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                    "error": r["error"],
                    "summary": r["summary"],
                }
        conn.close()
    except Exception:
        pass  # 表可能还不存在，跳过

    # ── 3. 任务清单 ──
    _raw = all_tasks()
    tasks = []
    for rt in _raw:
        t = dict(rt)
        t["task"] = t.pop("label")
        t["key"] = t.pop("name")
        tasks.append(t)

    def _badge(cls, text):
        colors = {
            "green":  "#16a34a", "yellow": "#b45309",
            "blue":   "#2563eb",
            "red":    "#dc2626", "gray":   "#6b7280",
        }
        return f'<span style="display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:600;background:{colors[cls]}18;color:{colors[cls]}">{text}</span>'

    for t in tasks:
        key = t["key"]
        has_cron = key in cron_tasks
        run = db_runs.get(key)

        if run and run["status"] == "running" and run["finished_at"] is None:
            # 任务专属超时检测 (与 orchestrator._check_timeouts 阈值一致)
            try:
                _API_TIMEOUTS = {"signals": 900, "execute": 600, "monitor": None,
                                 "attribution": 900, "weekly_eval": 7200}
                started = datetime.fromisoformat(run["started_at"])
                elapsed = (datetime.now() - started).total_seconds()
                limit = _API_TIMEOUTS.get(key)
                if limit is None and key == "monitor":
                    # monitor: 盘中不超时, 14:55 后才检查
                    now = datetime.now()
                    if now.hour >= 14 and now.minute >= 55:
                        limit = 1800
                if limit and elapsed > limit:
                    t["status_label"] = _badge("yellow", "运行超时")
                    t["status"] = "timeout"
                else:
                    t["status_label"] = _badge("blue", "运行中")
                    t["status"] = "running"
            except Exception:
                t["status_label"] = _badge("blue", "运行中")
                t["status"] = "running"
            t["last_run"] = (run["started_at"] or "")[:16].replace("T", " ")
        elif run and run["status"] == "ok":
            t["status_label"] = _badge("green", "今日已执行")
            t["status"] = "success"
            t["last_run"] = (run["finished_at"] or run["started_at"] or "")[:16].replace("T", " ")
        elif run and run["status"] == "failed":
            err = run["error"] or "未知错误"
            t["status_label"] = _badge("red", "今日失败")
            t["status"] = "error"
            t["error_msg"] = err[:120]
            t["last_run"] = (run["finished_at"] or run["started_at"] or "")[:16].replace("T", " ")
        elif run and run["status"] == "aborted":
            err = run["error"] or "任务异常终止"
            t["status_label"] = _badge("yellow", "异常终止")
            t["status"] = "aborted"
            t["error_msg"] = err[:120]
            t["last_run"] = (run["finished_at"] or run["started_at"] or "")[:16].replace("T", " ")
        elif has_cron:
            t["status_label"] = _badge("gray", "等待调度")
            t["status"] = "pending"
            t["last_run"] = "—"
        else:
            t["status_label"] = _badge("gray", "未配置")
            t["status"] = "unconfigured"
            t["last_run"] = "—"

        t["cron"] = "已配置" if has_cron else "未配置"

    return _api_response(data={"tasks": tasks})
@app.route("/api/metrics")
def api_metrics():
    """模板9 T1: 指标快照 (Prometheus 本地等价)."""
    from quant.monitor.metrics import metrics as _mm
    return _api_response(data=_mm.snapshot())

if __name__ == "__main__":
    port = int(_require_cfg("web.port"))
    logger.info(f"Web 服务启动于端口 {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
