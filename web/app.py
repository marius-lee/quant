"""量化选股 Web — 7 层架构监控仪表盘。

状态: web/shared.py 内存共享 (pipeline 写入, Flask 读取)
持久: data/trades.db (交易唯一真相源)
"""

import json, os, sqlite3
from data.store import market_conn  # P69: 统一连接层
from datetime import date, datetime
from flask import Flask, jsonify, render_template
# ── 进程退出埋点 ──
import atexit as _atexit, signal as _signal, sys as _sys, threading as _thr, os as _os
def _log_exit(reason: str = ""):
    try:
        from utils.logger import get_logger
        get_logger("web.app").warning(
            f"EXIT | reason={reason or 'unknown'} | pid={os.getpid()} | "
            f"thread={_thr.current_thread().name}")
    except Exception:
        print(f"[EXIT] {reason} pid={os.getpid()}", flush=True)

def _clean_exit(reason: str):
    """退出前清理 ProcessPoolExecutor 子进程, 防止孤儿泄漏。"""
    _log_exit(reason)
    _pid_file = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)),
                               "data", ".compute_pids")
    if _os.path.exists(_pid_file):
        try:
            with open(_pid_file) as _f:
                for _line in _f:
                    _pid = _line.strip()
                    if _pid:
                        try:
                            _os.kill(int(_pid), _signal.SIGTERM)
                        except ProcessLookupError:
                            pass
            _os.remove(_pid_file)
        except Exception:
            pass
    _sys.exit(0)

_atexit.register(_log_exit, "atexit")
_signal.signal(_signal.SIGTERM, lambda s, f: _clean_exit("SIGTERM"))
_signal.signal(_signal.SIGINT,  lambda s, f: _clean_exit("SIGINT"))

from utils.logger import get_logger

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
def _warm_factor_cache():
    try:
        from factor.stats_cache import get_cached_factor_stats
        logger.info("warming factor cache (background)...")
        # 不强制重算: 若 24h 缓存有效则直接返回, 否则自动计算
        get_cached_factor_stats(force_refresh=False)
        logger.info("factor cache warmup complete")
    except Exception as e:
        logger.warning(f"factor cache warmup skipped: {e}")
    # 启动三阶段调度器
    try:
        from quant.scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        logger.warning(f"scheduler start skipped: {e}")
threading.Thread(target=_warm_factor_cache, daemon=True).start()

TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")

def _capital(strategy: str) -> float:
    """从 strategy_config 表读本金。无记录时默认 5000 并自动写入。"""
    from data.trade_repo import TradeRepo
    repo = TradeRepo()
    cap = repo.get_initial_capital(strategy)
    if cap <= 0:
        cap = 5000.0
        repo.set_initial_capital(strategy, cap)
    return cap

from web.state_broker import broker
from web.shared import get_state, update_state  # deprecated, kept for compat


# ═══════════════════════════════════════════════════════════
# 核心 API
# ═══════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


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
    from factor.stats_cache import get_cached_factor_stats
    force = request.args.get("refresh", "false").lower() == "true"
    try:
        stats = get_cached_factor_stats(force_refresh=force)
        return _api_response(data=stats)
    except Exception as e:
        from utils.logger import get_logger
        get_logger("web.app").warning(f"Factor stats failed: {e}")
        return _api_response(error={"code": "FACTOR_ERROR", "message": str(e)})


@app.route("/api/positions")
def api_positions():
    """持仓 (从 trades.db 读取 — 通过 TradeRepo). ?strategy=过滤"""
    from flask import request
    strategy = request.args.get("strategy", "quant")
    limit = min(int(request.args.get("limit", 100)), 100)  # 模板6: max 100
    offset = int(request.args.get("offset", 0))
    positions = []
    try:
        from data.trade_repo import TradeRepo
        repo = TradeRepo(TRADE_DB)
        raw = repo.get_positions(strategy)
        # ── stock name + latest close lookup ──
        name_map = {}
        close_map = {}
        try:
            import sqlite3 as _sql
            market_db = os.path.join(os.path.dirname(__file__), "..", "data", "market.db")
            if os.path.exists(market_db):
                mc = _sql.connect(market_db)
                syms = [p["symbol"] for p in raw]
                if syms:
                    ph = ",".join(["?"] * len(syms))
                    rows = mc.execute(
                        f"SELECT symbol, name FROM stocks WHERE symbol IN ({ph})", syms
                    ).fetchall()
                    name_map = {r[0]: r[1] for r in rows}
                    # latest close price for each symbol
                    for sym in syms:
                        cr = mc.execute(
                            "SELECT close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)
                        ).fetchone()
                        if cr and cr[0]:
                            close_map[sym] = cr[0]
                mc.close()
        except Exception:
            pass
        for p in raw:
            px = p.get("price", 0)
            close_px = close_map.get(p["symbol"], px)
            positions.append({
                "symbol": p["symbol"], "price": px, "shares": p["shares"],
                "board_count": p.get("board_count", 0),
                "buy_time": p.get("buy_time", ""),
                "current": close_px,
                "pnl_pct": round((close_px / px - 1) * 100, 2) if px > 0 else 0,
                "value": round(p["shares"] * close_px, 2),
                "name": name_map.get(p["symbol"], ""), "change_pct": 0,
            })
    except Exception:
        logger.warning("api_positions: query failed", exc_info=_DEBUG)
        return _api_response(error={"code": "INTERNAL", "message": "positions query failed"}), 500
    paged = positions[offset:offset + limit]
    return _api_response(data={"positions": paged}, meta={"total": len(positions), "limit": limit, "offset": offset})


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
        from data.trade_repo import TradeRepo
        repo = TradeRepo(TRADE_DB)
        if strategy:
            raw_trades = repo.get_trades(strategy, limit=10000)  # 前端展示上限, 防止浏览器卡死, 非业务参数
            raw_positions = repo.get_positions(strategy)
        else:
            raw_trades = repo.get_trades(None, limit=10000)  # 前端展示上限, 防止浏览器卡死, 非业务参数
            raw_positions = repo.get_positions(None)
        trades = [{"date": (t.get("date") or "")[:19] if t.get("date") else "",
                    "symbol": t["symbol"], "side": t["side"], "price": t["price"],
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
    from data.trade_repo import TradeRepo
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
        conn.close()
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
    """实时行情 — 批量拉取新浪财经报价 + 止损扫描。

    ?symbols=000001,600036,430047 — 逗号分隔的股票代码列表
    返回: {quotes: {symbol: {price, change_pct, ...}}, status: "open"|"closed",
           stop_loss_triggers: [{symbol, price, drop_pct}]}
    仅在交易日 9:30-15:00 拉取, 否则返回空。
    盘中自动扫描 quant 策略持仓: 任一持仓从成本价跌超阈值即模拟卖出。
    """
    from flask import request
    from execution.quote import fetch_quotes, is_trading_time
    syms_str = request.args.get("symbols", "")
    if not syms_str:
        return _api_response(data={"quotes": {}})
    symbols = [s.strip() for s in syms_str.split(",") if s.strip() and len(s.strip()) == 6]
    if not symbols:
        return _api_response(data={"quotes": {}})
    if not is_trading_time():
        return _api_response(data={"quotes": {}, "status": "closed"})
    quotes = fetch_quotes(symbols)

    # ── 止损扫描 (quant 策略) ──
    stop_loss_triggers = []
    from config.loader import get as cfg
    sl_pct = cfg("risk.stop_loss_pct")
    try:
        import sqlite3
        tc = sqlite3.connect(TRADE_DB)
        positions = tc.execute("""
            SELECT symbol, SUM(shares) as net_shares,
                   SUM(price * shares) / SUM(shares) as cost_basis
            FROM sim_trades
            WHERE side='buy' AND strategy='quant'
            GROUP BY symbol
        """).fetchall()
        sells = tc.execute(
            "SELECT symbol, SUM(shares) FROM sim_trades WHERE side='sell' AND strategy='quant' GROUP BY symbol"
        ).fetchall()
        sell_map = {r[0]: r[1] for r in sells}
        today = __import__('datetime').date.today().isoformat()
        for sym, shares, cost_basis in positions:
            net = max(0, shares - sell_map.get(sym, 0))
            if net <= 0 or sym not in quotes:
                continue
            current_px = quotes[sym]["price"]
            drop = (current_px - cost_basis) / cost_basis if cost_basis > 0 else 0
            if drop <= -sl_pct:
                from execution.engine import ExecutionEngine, Order
                engine = ExecutionEngine()
                engine.execute(
                    [Order(symbol=sym, side="sell", shares=net, price=current_px, cost=0)],
                    today, strategy="quant"
                )
                stop_loss_triggers.append({
                    "symbol": sym, "price": round(current_px, 2),
                    "drop_pct": round(drop * 100, 1), "shares": net,
                })
                logger.warning("[SL] stop-loss via /api/quotes: %s drop=%.1f%% sold %d shares",
                               sym, drop * 100, net)
        tc.close()
    except Exception as e:
        logger.warning("[SL] stop-loss scan failed: %s", e)

    return _api_response(data={
        "quotes": quotes, "status": "open",
        "stop_loss_triggers": stop_loss_triggers,
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
    market_db = os.path.join(os.path.dirname(__file__), "..", "data", "market.db")
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
        mc.close()
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
    from config.loader import get as cfg
    strategy = request.args.get("strategy", "quant")
    tc = sqlite3.connect(TRADE_DB)
    sells = tc.execute("SELECT pnl FROM sim_trades WHERE side='sell' AND strategy=?", (strategy,)).fetchall()
    realized_pnl = sum(r[0] for r in sells if r[0])
    win_trades = sum(1 for r in sells if r[0] and r[0] > 0)
    total_sells = len(sells)
    win_rate = round(win_trades / total_sells * 100, 1) if total_sells > 0 else 0
    buys = tc.execute("SELECT COUNT(*) FROM sim_trades WHERE side='buy' AND strategy=?", (strategy,)).fetchone()[0]
    from data.trade_repo import TradeRepo; base = TradeRepo().get_initial_capital(strategy)
    capital = TradeRepo().get_cash(strategy) or base
    position_cost = tc.execute(
        "SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
        (strategy, strategy)).fetchone()[0]

    # 估值: ?quotes=true → 市价; 默认 → 账面成本
    use_quotes = request.args.get("quotes", "").lower() == "true"
    position_market_value = position_cost  # 默认用成本
    valuation_method = "book_cost"
    if use_quotes:
        try:
            pos_symbols = [r[0] for r in tc.execute(
                "SELECT symbol FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
                (strategy, strategy)).fetchall()]
            from execution.quote import fetch_quotes
            quotes = fetch_quotes(pos_symbols)
            shares_map = dict(tc.execute(
                "SELECT symbol, SUM(shares) FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?) GROUP BY symbol",
                (strategy, strategy)).fetchall())
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
            pass  # 报价不可用时回退到账面成本

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
            mc.close()
            if close_mv > 0:
                position_market_value = round(close_mv, 2)
                valuation_method = "latest_close"
        except Exception:
            pass

    total_asset = round(capital + position_market_value, 2)
    total_pnl = round(total_asset - base, 2)
    tc.close()
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
    def generate():
        try:
            # 先发当前状态
            yield f"data: {json.dumps(broker.get(), ensure_ascii=False)}\n\n"
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
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
        db = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "data", "market.db")
        conn = market_conn("ro")
        conn.execute("SELECT 1").fetchone()
        conn.close()
        status["checks"]["market_db"] = "ok"
    except Exception as e:
        status["checks"]["market_db"] = f"fail: {e}"
        status["status"] = "degraded"
    # 最近 pipeline 状态
    state = broker.get()
    status["pipeline"] = {
        "last_status": state.get("status", "unknown"),
        "last_progress": state.get("progress", ""),
        "last_trace_id": state.get("trace_id", ""),
    }
    from monitor.metrics import metrics as _mm
    status["metrics"] = _mm.snapshot()
    # 告警检查
    from monitor.alerts import check_alerts
    status["alerts"] = check_alerts(state, _mm.snapshot())
    return _api_response(data=status)

@app.route("/api/metrics")
def api_metrics():
    """模板9 T1: 指标快照 (Prometheus 本地等价)."""
    from monitor.metrics import metrics as _mm
    return _api_response(data=_mm.snapshot())

if __name__ == "__main__":
    from config.loader import get as cfg
    port = int(cfg("web.port", 8521))
    logger.info(f"Web 服务启动于端口 {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
