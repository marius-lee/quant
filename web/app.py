"""量化选股 Web — 7 层架构监控仪表盘。

状态: web/shared.py 内存共享 (pipeline 写入, Flask 读取)
持久: data/trades.db (交易唯一真相源)
"""

import json, os, sqlite3
from datetime import date, datetime
from flask import Flask, jsonify, render_template
from utils.logger import get_logger

logger = get_logger("web.app")
app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# 启动时异步预热因子评估缓存 (首次 /api/factors 请求免等待)
import threading
def _warm_factor_cache():
    try:
        from factor.stats_cache import get_cached_factor_stats
        logger.info("warming factor cache (background)...")
        get_cached_factor_stats(force_refresh=True)
        logger.info("factor cache warmup complete")
    except Exception as e:
        logger.warning(f"factor cache warmup skipped: {e}")
threading.Thread(target=_warm_factor_cache, daemon=True).start()

TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")

def _capital(strategy: str, fallback: float = None) -> float:
    """从 strategy_config 表读本金。如果表不存在则回退。"""
    if fallback is None:
        from config.loader import get as cfg
        fallback = float(cfg("backtest.initial_capital", 5000))
    try:
        conn = sqlite3.connect(TRADE_DB)
        row = conn.execute("SELECT initial_capital FROM strategy_config WHERE strategy=?", (strategy,)).fetchone()
        conn.close()
        return round(row[0], 2) if row else fallback
    except Exception:
        return fallback

from web.shared import get_state, update_state


# ═══════════════════════════════════════════════════════════
# 核心 API
# ═══════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    """当前完整状态: 资金 + 持仓 + 信号 + 暴露"""
    return jsonify(get_state())


@app.route("/api/factors")
def api_factors():
    """因子评估数据 — 为前端因子分析 Tab 提供 IC/IR/衰减/相关性。

    数据来源: factor/stats_cache.py → data/factor_cache.json (24h 过期)
    首次访问时自动计算 (约 30s), 后续 24h 内秒出。
    ?refresh=true 强制重新计算。
    """
    from flask import request
    from factor.stats_cache import get_cached_factor_stats
    force = request.args.get("refresh", "false").lower() == "true"
    try:
        stats = get_cached_factor_stats(force_refresh=force)
        return jsonify(stats)
    except Exception as e:
        from utils.logger import get_logger
        get_logger("web.app").warning(f"Factor stats failed: {e}")
        return jsonify({"error": str(e), "factors": [], "ic": [], "ic_ir": []})


@app.route("/api/positions")
def api_positions():
    """持仓 (从 trades.db 读取 — 唯一真相源). ?strategy=过滤"""
    from flask import request
    strategy = request.args.get("strategy", "quant")
    positions = []
    try:
        conn = sqlite3.connect(TRADE_DB)
        buys = conn.execute("""
            SELECT symbol, price, shares, board_count, date FROM sim_trades
            WHERE side='buy' AND strategy=? AND symbol NOT IN (
                SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?
            )
            ORDER BY date
        """, (strategy, strategy)).fetchall()
        for r in buys:
            sym, px, sh, board, dt = r[0], r[1], r[2], r[3], r[4]
            positions.append({
                "symbol": sym, "price": px, "shares": sh,
                "board_count": board, "date": dt,
                "current": px,          # 盘后用成本价; 交易时段前端用实时行情覆盖
                "pnl_pct": 0,           # 同上
                "value": round(sh * px, 2),
                "name": "",             # 前端可补; 后端暂不查stocks表
                "change_pct": 0,
            })
        conn.close()
    except Exception:
        pass
    return jsonify({"positions": positions})


@app.route("/api/trades")
def api_trades():
    """交易历史. ?strategy=过滤"""
    from flask import request
    strategy = request.args.get("strategy", "quant")
    trades = []
    positions = []
    try:
        conn = sqlite3.connect(TRADE_DB)
        if strategy:
            rows = conn.execute(
                "SELECT date, symbol, side, price, shares, pnl, pnl_pct FROM sim_trades WHERE strategy=? ORDER BY id",
                (strategy,)
            ).fetchall()
            buys = conn.execute("""
                SELECT symbol, price, shares, board_count, date FROM sim_trades
                WHERE side='buy' AND strategy=? AND symbol NOT IN (
                    SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?
                )
            """, (strategy, strategy)).fetchall()
        else:
            rows = conn.execute(
                "SELECT date, symbol, side, price, shares, pnl, pnl_pct FROM sim_trades ORDER BY id"
            ).fetchall()
            buys = conn.execute("""
                SELECT symbol, price, shares, board_count, date FROM sim_trades
                WHERE side='buy' AND symbol NOT IN (
                    SELECT symbol FROM sim_trades WHERE side='sell'
                )
            """).fetchall()
        trades = [{"date": r[0], "symbol": r[1], "side": r[2], "price": r[3],
                    "shares": r[4], "pnl": r[5], "pnl_pct": r[6]} for r in rows]
        positions = [{"symbol": r[0], "price": r[1], "shares": r[2],
                       "board_count": r[3], "date": r[4]} for r in buys]
        conn.close()
    except Exception:
        pass
    return jsonify({"trades": trades, "positions": positions})


@app.route("/api/trade", methods=["POST"])
def api_record_trade():
    """记录一笔交易 → trades.db (唯一真相源)"""
    from flask import request
    data = request.get_json(force=True)
    side = data.get("side")
    symbol = data.get("symbol")
    price = float(data.get("price", 0))
    shares = int(data.get("shares", 0))
    cost = float(data.get("cost", 0))

    if not symbol or price <= 0 or shares < 100:
        return jsonify({"ok": False, "error": "参数不完整"})

    today = date.today().isoformat()
    conn = sqlite3.connect(TRADE_DB)

    if side == "buy":
        conn.execute("""INSERT INTO sim_trades (date, symbol, side, price, shares, board_count)
                        VALUES (?,?,?,?,?,?)""",
                     (today, symbol, "buy", price, shares, data.get("board_count", 0)))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    elif side == "sell":
        pnl = (price - cost) * shares
        pnl_pct = round((price / cost - 1) * 100, 2) if cost > 0 else 0
        sells = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM sim_trades WHERE side='sell'").fetchone()[0]
        buys_cost = conn.execute(
            "SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='buy'"
        ).fetchone()[0]
        from config.loader import get as cfg
        base = float(cfg("backtest.initial_capital", 5000))
        capital_after = base + sells + pnl - buys_cost + (price * shares)
        conn.execute("""INSERT INTO sim_trades (date, symbol, side, price, shares, pnl, pnl_pct, capital_after)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (today, symbol, "sell", price, shares, round(pnl, 2), pnl_pct, round(capital_after, 2)))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "pnl": round(pnl, 2), "pnl_pct": pnl_pct})

    else:
        conn.close()
        return jsonify({"ok": False, "error": "side必须是buy或sell"})


@app.route("/api/state", methods=["POST"])
def api_update_state():
    """pipeline 更新状态"""
    from flask import request
    data = request.get_json(force=True)
    data["timestamp"] = datetime.now().isoformat()
    update_state(data)
    return jsonify({"ok": True})


@app.route("/api/quotes")
def api_quotes():
    """实时行情 — 批量拉取新浪财经报价, 为前端持仓页提供现价和涨跌幅。

    ?symbols=000001,600036,430047 — 逗号分隔的股票代码列表
    返回: {quotes: {symbol: {price, change_pct, name, high, low, volume}}}
    仅在交易日 9:30-15:00 拉取, 否则返回空。
    """
    from flask import request
    from execution.quote import fetch_quotes, is_trading_time
    syms_str = request.args.get("symbols", "")
    if not syms_str:
        return jsonify({"quotes": {}})
    symbols = [s.strip() for s in syms_str.split(",") if s.strip() and len(s.strip()) == 6]
    if not symbols:
        return jsonify({"quotes": {}})
    if not is_trading_time():
        return jsonify({"quotes": {}, "status": "closed"})
    quotes = fetch_quotes(symbols)
    return jsonify({"quotes": quotes, "status": "open"})


@app.route("/api/performance")
def api_performance():
    """累计绩效统计. ?strategy=quant"""
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
    base = float(cfg("backtest.initial_capital", 5000))
    row = tc.execute(
        "SELECT capital_after FROM sim_trades WHERE strategy=? AND capital_after IS NOT NULL ORDER BY id DESC LIMIT 1",
        (strategy,)).fetchone()
    capital = round(row[0], 2) if row else base
    position_cost = tc.execute(
        "SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
        (strategy, strategy)).fetchone()[0]
    total_asset = round(capital + position_cost, 2)
    total_pnl = round(total_asset - base, 2)
    tc.close()
    return jsonify({
        "realized_pnl": round(realized_pnl, 2),
        "total_pnl": total_pnl,
        "total_asset": total_asset,
        "total_sells": total_sells,
        "win_rate": win_rate,
        "total_buys": buys,
        "capital": round(capital, 2),
    })


if __name__ == "__main__":
    from config.loader import get as cfg
    port = int(cfg("web.port", 8521))
    logger.info(f"Web 服务启动于端口 {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
