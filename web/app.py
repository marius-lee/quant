"""量化选股 Web — 陈小群六模块体系前端。

状态: web/shared.py 内存共享 (intraday_runner 写入, Flask 读取)
持久: data/trades.db (持仓/交易唯一真相源)
"""

import json, os, sqlite3
from datetime import date, datetime
from flask import Flask, jsonify, render_template
from utils.logger import get_logger

logger = get_logger("web.app")
app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")

from web.shared import get_state, update_state


# ═══ 核心 API ═══

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    """当前完整状态: 情绪+信号+持仓+板块"""
    return jsonify(get_state())


@app.route("/api/mood")
def api_mood():
    """情绪周期"""
    state = get_state()
    return jsonify(state.get("mood", {}))


@app.route("/api/signals")
def api_signals():
    """当前信号列表"""
    state = get_state()
    return jsonify({
        "golden": state.get("golden_signals", []),
        "final": state.get("final_signals", []),
        "timestamp": state.get("timestamp", ""),
    })


@app.route("/api/sectors")
def api_sectors():
    """板块热度"""
    state = get_state()
    return jsonify(state.get("sectors", []))


@app.route("/api/positions")
def api_positions():
    """持仓 (从 trades.db 读取 — 唯一真相源)"""
    positions = []
    try:
        conn = sqlite3.connect(TRADE_DB)
        buys = conn.execute("""
            SELECT symbol, price, shares, board_count, date FROM sim_trades
            WHERE side='buy' AND symbol NOT IN (
                SELECT symbol FROM sim_trades WHERE side='sell'
            )
            ORDER BY date
        """).fetchall()
        for r in buys:
            positions.append({
                "symbol": r[0], "price": r[1], "shares": r[2],
                "board_count": r[3], "date": r[4],
            })
        conn.close()
    except Exception:
        pass
    return jsonify({"positions": positions, "exits": []})


@app.route("/api/trades")
def api_trades():
    """交易历史 + 当前持仓 (从 trades.db 读取)"""
    trades = []
    positions = []
    try:
        conn = sqlite3.connect(TRADE_DB)
        rows = conn.execute(
            "SELECT date, symbol, side, price, shares, pnl, pnl_pct FROM sim_trades ORDER BY id"
        ).fetchall()
        trades = [{"date": r[0], "symbol": r[1], "side": r[2], "price": r[3],
                    "shares": r[4], "pnl": r[5], "pnl_pct": r[6]} for r in rows]
        buys = conn.execute("""
            SELECT symbol, price, shares, board_count, date FROM sim_trades
            WHERE side='buy' AND symbol NOT IN (
                SELECT symbol FROM sim_trades WHERE side='sell'
            )
        """).fetchall()
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
        # 计算 capital_after
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


# ═══ 管理 API ═══

@app.route("/api/review")
def api_review():
    """盘后复盘 — 信号+交易分析"""
    from ops.review import generate_review
    return jsonify(generate_review())


@app.route("/api/state", methods=["POST"])
def api_update_state():
    """intraday_runner 更新瞬态"""
    from flask import request
    data = request.get_json(force=True)
    data["timestamp"] = datetime.now().isoformat()
    update_state(data)
    return jsonify({"ok": True})


if __name__ == "__main__":
    import threading
    from intraday_runner import run as intraday_run

    monitor = threading.Thread(target=intraday_run, daemon=True, name="intraday")
    monitor.start()
    logger.info("日内监控线程已启动")

    from config.loader import get as cfg
    port = int(cfg("web.port", 8521))
    app.run(host="0.0.0.0", port=port, debug=False)
