"""大盘择时策略 — 沪深300趋势判断, 涨则全仓, 跌则空仓.
来源: 聚宽天梯 脆脆鲨l (年化107.87%, 回撤21.65%)
链接: joinquant.com/view/community/detail/8034895184bb7af92849e61274ebc065
"""
import sqlite3, os
from datetime import date, timedelta

STRATEGY = "timing"
DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")
SYMBOL = "510300"  # 沪深300ETF
THRESHOLD = 0.02   # 20日涨幅阈值 (来源: 聚宽天梯)


def get_signal() -> dict:
    """计算信号: 沪深300近20日涨跌."""
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT close FROM daily WHERE symbol=? AND date >= ? ORDER BY date",
        (SYMBOL, (date.today() - timedelta(days=40)).isoformat())
    ).fetchall()
    conn.close()

    if len(rows) < 20:
        return {"action": "hold", "reason": "数据不足"}

    ret_20d = (rows[-1][0] - rows[-20][0]) / rows[-20][0] if rows[-20][0] > 0 else 0
    if ret_20d >= THRESHOLD:
        return {"action": "buy", "symbol": SYMBOL, "name": "沪深300",
                "ret_20d": round(ret_20d, 4)}
    else:
        return {"action": "sell", "reason": f"20日涨幅{ret_20d:.1%}<{THRESHOLD:.0%}"}


def record_trade(symbol, name, price, shares, side="buy"):
    conn = sqlite3.connect(TRADE_DB)
    cost = price * shares
    fee = max(cost * 0.0003, 5)
    pnl = None
    if side == "sell":
        buy = conn.execute(
            "SELECT price, shares FROM sim_trades WHERE symbol=? AND side='buy' AND strategy=? ORDER BY id DESC LIMIT 1",
            (symbol, STRATEGY)
        ).fetchone()
        if buy:
            pnl = round((price - buy[0]) * shares - fee - max(buy[0]*buy[1]*0.0003,5) - price*shares*0.001, 2)
    conn.execute(
        "INSERT INTO sim_trades (date, symbol, side, price, shares, strategy) VALUES (?,?,?,?,?,?)",
        (date.today().isoformat(), symbol, side, price, shares, STRATEGY)
    )
    conn.commit()
    conn.close()
    return pnl


def execute():
    """执行策略: 根据信号买卖."""
    sig = get_signal()
    tc = sqlite3.connect(TRADE_DB)
    mc = sqlite3.connect(DB)
    pos = tc.execute(
        "SELECT symbol,price,shares FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
        (STRATEGY, STRATEGY)
    ).fetchall()

    if sig["action"] == "buy":
        if pos and pos[0][0] == SYMBOL:
            tc.close(); mc.close(); return True
        for r in pos:
            record_trade(r[0], "", r[1], r[2], "sell")
        row = mc.execute("SELECT close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1", (SYMBOL,)).fetchone()
        if row:
            bs = tc.execute("SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='buy' AND strategy=?",(STRATEGY,)).fetchone()[0]
            ss = tc.execute("SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='sell' AND strategy=?",(STRATEGY,)).fetchone()[0]
            cap = 5000.0 - bs + ss
            lots = int(cap / (row[0]*100 + max(row[0]*100*0.0003,5)))
            if lots >= 1:
                record_trade(SYMBOL, "沪深300", row[0], lots*100, "buy")

    elif sig["action"] == "sell" and pos:
        for r in pos:
            row = mc.execute("SELECT close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1", (r[0],)).fetchone()
            if row:
                record_trade(r[0], "", row[0], r[2], "sell")

    tc.close(); mc.close()
    return True


def get_state() -> dict:
    conn = sqlite3.connect(TRADE_DB)
    pos = conn.execute(
        "SELECT symbol,price,shares FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
        (STRATEGY, STRATEGY)
    ).fetchall()
    pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM sim_trades WHERE side='sell' AND strategy=?",(STRATEGY,)).fetchone()[0]
    conn.close()

    positions = []
    if pos:
        try:
            from execution.quote import fetch_quotes
            qs = fetch_quotes([r[0] for r in pos])
            for r in pos:
                sym, cost, shares = r[0], r[1], r[2]
                q = qs.get(sym, {})
                cur = q.get("price", cost) if q else cost
                positions.append({"symbol": sym, "name": "沪深300", "shares": shares,
                                  "price": cost, "current": round(cur,2),
                                  "pnl_pct": round((cur/cost-1)*100,2),
                                  "value": round(shares*cur,2)})
        except Exception:
            for r in pos:
                positions.append({"symbol": r[0], "name": "沪深300", "shares": r[2],
                                  "price": r[1], "current": r[1], "pnl_pct": 0,
                                  "value": round(r[2]*r[1],2)})

    sig = get_signal()
    return {"positions": positions, "realized_pnl": round(pnl,2), "signal": sig,
            "capital": 5000.0 + pnl - sum(r[1]*r[2] for r in pos)}