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

import web.shared
import importlib
importlib.reload(web.shared)
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
    """交易历史 + 当前持仓 (从 trades.db 读取). ?strategy=过滤"""
    from flask import request
    strategy = request.args.get("strategy", "")
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

@app.route("/api/performance")
def api_performance():
    """累计绩效统计. ?strategy=chen|etf|smallcap|timing"""
    from flask import request
    from config.loader import get as cfg
    strategy = request.args.get("strategy", "chen")
    tc = sqlite3.connect(TRADE_DB)
    sells = tc.execute("SELECT pnl FROM sim_trades WHERE side='sell' AND strategy=?", (strategy,)).fetchall()
    realized_pnl = sum(r[0] for r in sells if r[0])
    win_trades = sum(1 for r in sells if r[0] and r[0] > 0)
    total_sells = len(sells)
    win_rate = round(win_trades / total_sells * 100, 1) if total_sells > 0 else 0
    buys = tc.execute("SELECT COUNT(*) FROM sim_trades WHERE side='buy' AND strategy=?", (strategy,)).fetchone()[0]
    tc.close()
    # 从对应策略 get_state() 或 sim_trades 直接计算 total_asset
    if strategy.startswith("chen_"):
        # sizer策略: 直接从 sim_trades 计算
        bs = tc.execute("SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='buy' AND strategy=?",
                       (strategy,)).fetchone()[0]
        ss = tc.execute("SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='sell' AND strategy=?",
                       (strategy,)).fetchone()[0]
        total_asset = round(5000.0 - bs + ss + realized_pnl, 2)
    elif strategy == "etf":
        from strategies.etf_rotation import get_state as gs
        total_asset = gs().get("total_asset", 5000.0)
    elif strategy == "smallcap":
        from strategies.smallcap_rotation import get_state as gs
        total_asset = gs().get("total_asset", 5000.0)
    elif strategy == "timing":
        from strategies.market_timing import get_state as gs
        total_asset = gs().get("total_asset", 5000.0)
    else:
        from web.shared import get_state as gs
        total_asset = gs().get("total_asset", 5000.0)
    base = float(cfg("backtest.initial_capital", 5000))
    total_pnl = round(total_asset - base, 2)
    return jsonify({
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(total_pnl - realized_pnl, 2),
        "total_pnl": total_pnl,
        "total_sells": total_sells,
        "win_rate": win_rate,
        "total_buys": buys,
    })


@app.route("/api/performance/icir")
def api_performance_icir():
    """Grinold & Kahn IC/IR/BR 指标. ?strategy=chen|etf|smallcap|timing &force=0|1"""
    from flask import request
    strategy = request.args.get("strategy", "chen")
    force = request.args.get("force", "0") == "1"
    from ops.performance import compute_strategy_metrics
    return jsonify(compute_strategy_metrics(strategy, force=force))


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


# ═══ 策略路由: ETF轮动 / 小市值轮动 ═══

@app.route("/etf")
def etf_page():
    return render_template("etf.html")

@app.route("/smallcap")
def smallcap_page():
    return render_template("smallcap.html")

@app.route("/api/etf/state")
def api_etf_state():
    from strategies.etf_rotation import get_state
    return jsonify(get_state())

@app.route("/timing")
def timing_page():
    return render_template("timing.html")

@app.route("/arena")
def arena_page():
    return render_template("arena.html")

@app.route("/api/debug")
def api_debug():
    from web.shared import _init_state
    s = _init_state()
    return jsonify({"capital": s["capital"], "total": s["total_asset"], "md5": "1db46253b1e90d0d31d0f5bc31d411a3"})

@app.route("/api/timing/state")
def api_timing_state():
    from strategies.market_timing import get_state
    return jsonify(get_state())

@app.route("/api/smallcap/state")
def api_smallcap_state():
    from strategies.smallcap_rotation import get_state
    return jsonify(get_state())


@app.route("/api/etf/execute", methods=["POST"])
def api_etf_execute():
    """执行ETF轮动: 清仓→全买第1名"""
    from strategies.etf_rotation import get_signal, record_trade, POOL, STRATEGY
    import sqlite3
    sig = get_signal()
    if sig["action"] not in ("buy", "defense"):
        return jsonify({"ok": False, "error": "信号不足,不执行"})

    conn = sqlite3.connect(TRADE_DB)
    # 清仓当前持仓
    sold = []
    for r in conn.execute(
        "SELECT symbol,price,shares FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
        (STRATEGY, STRATEGY)
    ).fetchall():
        pnl = record_trade(r[0], "", r[1], r[2], "sell")
        sold.append({"symbol": r[0], "pnl": pnl})

    # 买入新标的
    target = sig["buy"]
    name = sig.get("name", "")
    row = conn.execute("SELECT close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1", (target,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": f"无{target}日线数据"})

    price = row[0]
    # 计算可买手数
    buys = conn.execute("SELECT SUM(price*shares) FROM sim_trades WHERE side='buy' AND strategy=?", (STRATEGY,)).fetchone()[0] or 0
    sells = conn.execute("SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='sell' AND strategy=?", (STRATEGY,)).fetchone()[0]
    capital = 5000.0 - buys + sells
    lots = int(capital / (price * 100 + max(price * 100 * 0.0003, 5)))
    if lots < 1:
        conn.close()
        return jsonify({"ok": False, "error": f"资金不足({capital:.0f}), 无法买{target}"})

    record_trade(target, name, price, lots * 100, "buy")
    conn.close()
    return jsonify({"ok": True, "sold": sold, "bought": {"symbol": target, "price": price, "shares": lots * 100}})


@app.route("/api/smallcap/execute", methods=["POST"])
def api_smallcap_execute():
    """执行小市值轮动: 清仓→买Top5"""
    from strategies.smallcap_rotation import get_signal, record_trade, STRATEGY
    import sqlite3
    sig = get_signal()
    if sig["action"] != "rotate":
        return jsonify({"ok": False, "error": sig.get("reason", "非轮动信号")})

    conn = sqlite3.connect(TRADE_DB)
    # 清仓
    sold = []
    for r in conn.execute(
        "SELECT symbol,price,shares FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
        (STRATEGY, STRATEGY)
    ).fetchall():
        pnl = record_trade(r[0], "", r[1], r[2], "sell")
        sold.append({"symbol": r[0], "pnl": pnl})

    # 等权买入
    picks = sig.get("picks", [])
    if not picks:
        conn.close()
        return jsonify({"ok": False, "error": "无选股结果"})

    bs = conn.execute("SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='buy' AND strategy=?",(STRATEGY,)).fetchone()[0]
    ss = conn.execute("SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='sell' AND strategy=?",(STRATEGY,)).fetchone()[0]
    capital = 5000.0 - bs + ss
    bought = []
    for p in picks:
        cost_per_lot = p["close"] * 100 + max(p["close"] * 100 * 0.0003, 5)
        lots = int(capital / cost_per_lot)
        if lots < 1:
            continue
        record_trade(p["symbol"], p.get("name", ""), p["close"], lots * 100, "buy")
        capital -= cost_per_lot * lots
        bought.append({"symbol": p["symbol"], "price": p["close"], "shares": lots * 100})
    conn.close()
    return jsonify({"ok": True, "sold": sold, "bought": bought})


def _execute_etf():
    """ETF轮动执行(调度器调用)"""
    from strategies.etf_rotation import get_signal, record_trade, STRATEGY as S
    sig = get_signal()
    if sig["action"] not in ("buy", "defense"): return False
    mc = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db"))
    tc = sqlite3.connect(TRADE_DB)
    for r in tc.execute("SELECT symbol,price,shares FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)", (S,S)).fetchall():
        record_trade(r[0], "", r[1], r[2], "sell")
    t = sig["buy"]
    row = mc.execute("SELECT close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1",(t,)).fetchone()
    if row:
        bs = tc.execute("SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='buy' AND strategy=?",(S,)).fetchone()[0]
        ss = tc.execute("SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='sell' AND strategy=?",(S,)).fetchone()[0]
        cap = 5000.0 - bs + ss
        lots = int(cap / (row[0]*100 + max(row[0]*100*0.0003,5)))
        if lots >= 1: record_trade(t, sig.get("name",""), row[0], lots*100, "buy")
    mc.close()
    tc.close()
    return True


def _execute_smallcap():
    """小市值轮动执行(调度器调用)"""
    from strategies.smallcap_rotation import get_signal, record_trade, STRATEGY as S
    sig = get_signal()
    if sig["action"] != "rotate": return False
    conn = sqlite3.connect(TRADE_DB)
    for r in conn.execute("SELECT symbol,price,shares FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",(S,S)).fetchall():
        record_trade(r[0], "", r[1], r[2], "sell")
    picks = sig.get("picks",[])
    if picks:
        bs = conn.execute("SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='buy' AND strategy=?",(S,)).fetchone()[0]
        ss = conn.execute("SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='sell' AND strategy=?",(S,)).fetchone()[0]
        capital = 5000.0 - bs + ss
        for p in picks:
            cost_per_lot = p["close"]*100 + max(p["close"]*100*0.0003,5)
            lots = int(capital / cost_per_lot)
            if lots >= 1:
                record_trade(p["symbol"], p.get("name",""), p["close"], lots*100, "buy")
                capital -= cost_per_lot * lots
    conn.close()
    return True


if __name__ == "__main__":
    import threading, time as _time
    from intraday_runner import run as intraday_run
    from datetime import date as _date, datetime as _dt
    from execution.calendar import is_trading_day

    monitor = threading.Thread(target=intraday_run, daemon=True, name="intraday")
    monitor.start()
    logger.info("日内监控线程已启动")

    def _scheduler():
        """策略调度: 每日9:00-10:00间执行ETF+小市值, 各自独立重试直到成功"""
        _time.sleep(60)
        while True:
            try:
                now = _dt.now()
                if not is_trading_day() or now.hour < 9 or now.hour >= 15:
                    _time.sleep(60); continue
                if 9 <= now.hour < 15:
                    tc = sqlite3.connect(TRADE_DB)
                    etf_done = tc.execute("SELECT COUNT(*) FROM sim_trades WHERE date=? AND strategy='etf'",(now.strftime("%Y-%m-%d"),)).fetchone()[0] > 0
                    sc_done = tc.execute("SELECT COUNT(*) FROM sim_trades WHERE date=? AND strategy='smallcap'",(now.strftime("%Y-%m-%d"),)).fetchone()[0] > 0
                    tm_done = tc.execute("SELECT COUNT(*) FROM sim_trades WHERE date=? AND strategy='timing'",(now.strftime("%Y-%m-%d"),)).fetchone()[0] > 0
                    tc.close()
                    if not etf_done:
                        if _execute_etf():
                            logger.info("ETF轮动: 日频执行完成")
                    if not sc_done:
                        if _execute_smallcap():
                            logger.info("小市值轮动: 日频执行完成")
                    if not tm_done:
                        from strategies.market_timing import execute as _execute_timing
                        if _execute_timing():
                            logger.info("大盘择时: 日频执行完成")
            except Exception:
                pass
            _time.sleep(60)

    scheduler = threading.Thread(target=_scheduler, daemon=True, name="scheduler")
    scheduler.start()
    logger.info("策略调度线程已启动")

    from config.loader import get as cfg
    port = int(cfg("web.port", 8521))
    app.run(host="0.0.0.0", port=port, debug=False)
