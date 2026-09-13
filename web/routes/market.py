"""Web routes — market group."""
import json, sqlite3
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime
from web.app import app, _require_token, _api_response, _require_cfg, cfg, logger
from web.services import PositionService, BacktestService, StockService, SignalService
from web.shared import get_state, update_state
from quant.config.paths import TRADE_DB, MARKET_DB, BACKTEST_DB
from quant.config.loader import get as _cfg
from quant.data.store import market_conn
from quant.core.state_broker import broker

@app.get("/api/positions")
def api_positions(request: Request):
    """持仓 — broker 数据 (含 state_broker 实时现价 overlay)。"""
    strategy = request.query_params.get("strategy", "quant")
    limit = min(int(request.query_params.get("limit", 100)), 100)
    offset = int(request.query_params.get("offset", 0))
    try:
        state = broker.get()
        positions = state.get("positions", [])
        if strategy and strategy != "quant":
            positions = [p for p in positions if p.get("strategy", "quant") == strategy]
        paged = positions[offset:offset + limit]
        return _api_response(data={"positions": paged}, meta={"total": len(positions), "limit": limit, "offset": offset})
    except Exception:
        logger.warning("api_positions failed", exc_info=True)
        return _api_response(error={"code": "INTERNAL", "message": "positions query failed"}, status_code=500)


@app.get("/api/trades")
def api_trades(request: Request):
    """交易历史. ?strategy=过滤&limit=N&offset=M (模板6分页)"""
    strategy = request.query_params.get("strategy", "quant")
    limit = min(int(request.query_params.get("limit", 100)), 100)
    offset = int(request.query_params.get("offset", 0))
    trades = []
    positions = []
    try:
        from quant.data.repos import TradeRepo
        repo = TradeRepo(TRADE_DB)
        if strategy:
            raw_trades = repo.get_trades(strategy, limit=10000)  # 前端展示上限, 防止浏览器卡死, 非业务参数
            raw_positions = repo.get_positions(strategy)
        else:
            raw_trades = repo.get_trades(None, limit=10000)  # 前端展示上限, 防止浏览器卡死, 非业务参数
            raw_positions = repo.get_positions(None)
        # enrich with stock names via StockService (P2-5: SQL 封装)
        _names = {}
        try:
            _syms = list(set(t["symbol"] for t in (raw_trades or [])))
            _names = StockService.get_names(_syms)
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
        return _api_response(error={"code": "INTERNAL", "message": "trades query failed"}, status_code=500)
    return _api_response(data={"trades": trades[offset:offset + limit], "positions": positions}, meta={"total_trades": len(trades), "limit": limit, "offset": offset})


@app.post("/api/trade")
async def api_record_trade(request: Request):
    """记录一笔交易 → trades.db (手动交易，strategy='manual')"""
    _auth = _require_token(request)
    if _auth:
        return _auth
    data = await request.json()
    side = data.get("side")
    strategy = "manual"
    symbol = data.get("symbol")
    try:
        price = float(data.get("price", 0))
        shares = int(data.get("shares", 0))
        cost = float(data.get("cost", 0))
    except (TypeError, ValueError):
        return _api_response(error={"code": "INVALID_PARAMETER", "message": "price/shares/cost 格式错误", "field": "price/shares/cost"}, status_code=400)

    # 模板 1: 输入边界校验
    if not symbol or not isinstance(symbol, str) or len(symbol) != 6:
        return _api_response(error={"code": "INVALID_PARAMETER", "message": "symbol 必须是6位代码", "field": "symbol"}, status_code=400)
    if price <= 0 or price > 100000:
        return _api_response(error={"code": "INVALID_PARAMETER", "message": "price 超出范围 (0, 100000]", "field": "price"}, status_code=400)
    if shares < 100 or shares % 100 != 0:
        return _api_response(error={"code": "INVALID_PARAMETER", "message": "shares 必须是100的整数倍且≥100", "field": "shares"}, status_code=400)
    if side not in ("buy", "sell"):
        return _api_response(error={"code": "INVALID_PARAMETER", "message": "side 必须是 buy 或 sell", "field": "side"}, status_code=400)

    today = date.today().isoformat()
    from quant.data.repos import TradeRepo
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
        return _api_response(error={"code": "INVALID_PARAMETER", "message": "side必须是buy或sell", "field": "side"}, status_code=400)


@app.post("/api/state")
async def api_update_state(request: Request):
    """pipeline 更新状态"""
    _auth = _require_token(request)
    if _auth:
        return _auth
    data = await request.json()
    data["timestamp"] = datetime.now().isoformat()
    update_state(data)
    return _api_response(data={"ok": True})


@app.get("/api/quotes")
def api_quotes():
    """实时行情 — 批量拉取新浪财经报价。

    ?symbols=000001,600036,430047 — 逗号分隔的股票代码列表
    返回: {quotes: {symbol: {price, change_pct, ...}}, status: "open"|"closed"}
    仅在交易日 9:30-15:00 拉取, 否则返回空。
    止盈止损已移到 monitor.py 盘中风控统一管理。
    """
    from quant.execution.quote import fetch_quotes, is_trading_time
    syms_str = request.query_params.get("symbols", "")
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



@app.get("/api/risk")
def api_risk():
    """风险暴露 — 持仓波动率 & 最大回撤 (60日滚动).

    ?symbols=002072,002767 — 需要计算的持仓代码列表
    返回: {symbols: [{symbol, weight_pct, annual_vol_pct, max_dd_pct}]}
    """
    syms_str = request.query_params.get("symbols", "")
    symbols = [s.strip() for s in syms_str.split(",") if s.strip() and len(s.strip()) == 6]
    if not symbols:
        return _api_response(data={"symbols": []})

    import sqlite3, math
    market_db = MARKET_DB
    result = []
    mc = market_conn("ro")
    try:
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
            annual_vol = math.sqrt(variance * _require_cfg("market.annual_trading_days")) * 100  # annualized %
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
        return _api_response(error={"code": "INTERNAL", "message": str(e)},status_code=500)
    finally:
        mc.close()

    # Merge with portfolio weights from state
    state = broker.get()
    positions = state.get("positions", [])
    pos_map = {p["symbol"]: p.get("value", 0) for p in positions}
    total_val = sum(pos_map.values())
    for r in result:
        r["weight_pct"] = round(pos_map.get(r["symbol"], 0) / total_val * 100, 1) if total_val > 0 else 0

    # ── portfolio-level summary: VaR/CVaR/ MaxDD from daily_equity ──
    summary = {"var_95_pct": 0.0, "cvar_95_pct": 0.0, "max_dd_pct": 0.0}
    try:
        from quant.risk.var import historical_var, historical_cvar
        import pandas as pd
        # Compute from daily_equity first
        tconn = sqlite3.connect(TRADE_DB)
        eq_rows = tconn.execute(
            "SELECT date, total_equity FROM daily_equity ORDER BY date ASC LIMIT 120"
        ).fetchall()
        tconn.close()
        if len(eq_rows) >= 5:
            eq = pd.DataFrame(eq_rows, columns=["date", "total_equity"])
            eq["ret"] = eq["total_equity"].pct_change()
            rets = eq["ret"].dropna()
            if len(rets) >= 10:
                eq["peak"] = eq["total_equity"].cummax()
                eq["dd"] = (eq["peak"] - eq["total_equity"]) / eq["peak"]
                max_dd_series = eq["dd"].max()
                summary = {"var_95_pct": round(float(historical_var(rets)), 2),
                           "cvar_95_pct": round(float(historical_cvar(rets)), 2),
                           "max_dd_pct": round(float(max_dd_series * 100), 1)}

        # Fallback: 从持仓个股数据估算 (daily_equity 数据不足时)
        if summary["var_95_pct"] == 0 and summary["max_dd_pct"] == 0 and result:
            wtd_vol = sum(r["annual_vol_pct"] * r["weight_pct"] / 100 for r in result if r["weight_pct"] > 0)
            wtd_mdd = max((r["max_dd_pct"] for r in result if r["weight_pct"] > 0), default=0)
            summary = {"var_95_pct": round(wtd_vol * 1.65 / 100, 2),
                       "cvar_95_pct": round(wtd_vol * 2.0 / 100, 2),
                       "max_dd_pct": round(wtd_mdd, 1)}
    except Exception as e:
        logger.warning("Cannot compute portfolio VaR/CVaR: %s", e)

    return _api_response(data={"symbols": result, "summary": summary,
                                "total_value": round(total_val, 2)})


@app.get("/api/stress")
def api_stress():
    """Stress Test — 历史极端场景冲击测试."""
    try:
        state = broker.get()
        positions = state.get("positions", [])
        capital = state.get("total_asset", state.get("capital", 5000))
        # v536: 原 from quant.risk.stress_test import run_stress_tests — 该模块
        # 已在 v438 删除, 端点必 500 (前端在调) → 改用 var.stress_test (活代码,
        # backtest/loop.py:893 同源); weights = 各持仓市值 (RMB)
        from quant.risk.var import stress_test
        weights = {
            p["symbol"]: p.get("price", 0) * p.get("shares", 0)
            for p in positions if p.get("symbol")
        }
        result = stress_test(positions, weights)
        return _api_response(data={"capital": capital, "scenarios": result})
    except Exception as e:
        logger.warning(f"api_stress failed: {e}")
        return _api_response(error={"code": "INTERNAL", "message": str(e)},status_code=500)


@app.get("/api/performance")
def api_performance(request: Request):
    """累计绩效统计. ?strategy=quant&quotes=true (quotes=true 用市价估值)"""
    strategy = request.query_params.get("strategy", "quant")
    tc = sqlite3.connect(TRADE_DB)
    sells = tc.execute("SELECT pnl FROM sim_trades WHERE side='sell' AND strategy=?", (strategy,)).fetchall()
    realized_pnl = sum(r[0] for r in sells if r[0])
    win_trades = sum(1 for r in sells if r[0] and r[0] > 0)
    total_sells = len(sells)
    win_rate = round(win_trades / total_sells * 100, 1) if total_sells > 0 else 0
    buys = (tc.execute("SELECT COUNT(*) FROM sim_trades WHERE side='buy' AND strategy=?", (strategy,)).fetchone() or [0])[0]
    from quant.data.repos import TradeRepo; base = TradeRepo().get_initial_capital(strategy)
    capital = TradeRepo().get_cash(strategy) or base
    pos_row = tc.execute(
        "SELECT COALESCE(SUM(CASE WHEN side='buy' THEN price*shares ELSE -price*shares END),0) FROM sim_trades WHERE strategy=? HAVING SUM(CASE WHEN side='buy' THEN shares ELSE -shares END) > 0",
        (strategy,)).fetchone()
    position_cost = pos_row[0] if pos_row else 0

    # 估值: ?quotes=true → 市价; 默认 → 最新收盘, 均失败则账面成本 (test-v203)
    use_quotes = request.query_params.get("quotes", "").lower() == "true"
    position_market_value = position_cost  # 默认用成本
    valuation_method = "book_cost"
    # shares_map 提前填充, latest_close fallback 不依赖 use_quotes
    shares_map = dict(tc.execute(
        "SELECT symbol, SUM(CASE WHEN side='buy' THEN shares ELSE -shares END) AS net_shares FROM sim_trades WHERE strategy=? GROUP BY symbol HAVING SUM(CASE WHEN side='buy' THEN shares ELSE -shares END) > 0",
        (strategy,)).fetchall())
    if use_quotes:
        try:
            pos_symbols = [r[0] for r in tc.execute(
                "SELECT symbol FROM sim_trades WHERE strategy=? GROUP BY symbol HAVING SUM(CASE WHEN side='buy' THEN shares ELSE -shares END) > 0",
                (strategy,)).fetchall()]
            from quant.execution.quote import fetch_quotes
            quotes = fetch_quotes(pos_symbols)
            if quotes:
                pos_share_map = dict(tc.execute(
                    "SELECT symbol, SUM(CASE WHEN side='buy' THEN shares ELSE -shares END) AS net_shares FROM sim_trades WHERE strategy=? GROUP BY symbol HAVING SUM(CASE WHEN side='buy' THEN shares ELSE -shares END) > 0",
                    (strategy,)).fetchall())
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


