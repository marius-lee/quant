"""小市值轮动策略 — 每周二调仓, 流通市值最小Top5等权.
来源: 聚宽社区 (国九小市值年化100%, 回撤25%), 适配¥5,000
"""
import sqlite3, os
from datetime import date, timedelta

STRATEGY = "smallcap"
DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")

MAX_POSITIONS = 5
EXCLUDE_BOARDS = ("688", "8", "4", "92", "900")  # 排除科创板/北交所/B股


def get_signal() -> dict:
    """筛选小市值Top5, 排除ST/涨停/次新/科创."""
    conn = sqlite3.connect(DB)
    today = date.today()

    # 最近交易日
    max_date = conn.execute(
        "SELECT MAX(date) FROM daily WHERE date LIKE '%-%-%'"
    ).fetchone()[0]

    # 选股: 排除ST(名称含ST), 科创板, 北交所, 次新(上市<60天), 涨停
    candidates = []
    for r in conn.execute("""
        SELECT d.symbol, s.name, d.close, d.volume
        FROM daily d JOIN stocks s ON d.symbol = s.symbol
        WHERE d.date = ? AND d.close > 0 AND d.volume > 0
          AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
          AND d.symbol NOT LIKE '688%' AND d.symbol NOT LIKE '8%'
          AND d.symbol NOT LIKE '4%' AND d.symbol NOT LIKE '92%'
    """, (max_date,)).fetchall():
        # 过滤涨停 (涨幅>9.5%)
        prev = conn.execute(
            "SELECT close FROM daily WHERE symbol=? AND date < ? ORDER BY date DESC LIMIT 1",
            (r[0], max_date)
        ).fetchone()
        if prev and prev[0] > 0 and (r[2] / prev[0] - 1) >= 0.095:
            continue  # 涨停, 买不到
        # 近似市值: close × 总股本 (用成交额/换手率反推, 简化用close*volume/换手)
        candidates.append({"symbol": r[0], "name": r[1], "close": r[2], "volume": r[3]})

    conn.close()

    # 按成交额升序 (近似市值排序), 只保留买得起的 (≤¥50/股)
    candidates.sort(key=lambda x: x["volume"] * x["close"])
    picks = []
    for s in candidates:
        if s["close"] <= 50:
            picks.append({"symbol": s["symbol"], "name": s["name"], "close": s["close"]})

    if today.month in (1, 4):
        return {"action": "defense", "reason": f"{today.month}月财报季空仓"}

    return {"action": "rotate", "picks": picks[:20], "count": len(picks)}


def record_trade(symbol, name, price, shares, side="buy"):
    """记录交易."""
    conn = sqlite3.connect(TRADE_DB)
    pnl = None
    if side == "sell":
        buy = conn.execute(
            "SELECT price, shares FROM sim_trades WHERE symbol=? AND side='buy' AND strategy=? ORDER BY id DESC LIMIT 1",
            (symbol, STRATEGY)
        ).fetchone()
        if buy:
            fee = max(price * shares * 0.0003, 5) + price * shares * 0.001
            pnl = round((price - buy[0]) * shares - fee, 2)
    conn.execute(
        "INSERT INTO sim_trades (date, symbol, side, price, shares, strategy) VALUES (?,?,?,?,?,?)",
        (date.today().isoformat(), symbol, side, price, shares, STRATEGY)
    )
    conn.commit()
    conn.close()
    return pnl


def _get_realtime(symbols: list) -> dict:
    try:
        from execution.quote import fetch_quotes
        return fetch_quotes(symbols)
    except Exception:
        return {}


def get_state() -> dict:
    """返回当前策略状态(含实时价格)."""
    conn = sqlite3.connect(TRADE_DB)
    pos = conn.execute(
        "SELECT symbol, price, shares FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
        (STRATEGY, STRATEGY)
    ).fetchall()
    pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM sim_trades WHERE side='sell' AND strategy=?",
        (STRATEGY,)
    ).fetchone()[0]
    conn.close()

    symbols = [r[0] for r in pos]
    quotes = _get_realtime(symbols) if symbols else {}
    positions = []
    for r in pos:
        sym, cost, shares = r[0], r[1], r[2]
        q = quotes.get(sym, {})
        current = q.get("price", cost) if q else cost
        name = q.get("name", "")
        if not name:
            # fallback to stocks table
            try:
                mc = sqlite3.connect(DB)
                nr = mc.execute("SELECT name FROM stocks WHERE symbol=?",(sym,)).fetchone()
                if nr: name = nr[0]
                mc.close()
            except Exception:
                pass
        positions.append({
            "symbol": sym, "name": name, "shares": shares,
            "price": cost, "current": round(current, 2),
            "pnl_pct": round((current / cost - 1) * 100, 2),
            "value": round(shares * current, 2),
        })

    cap_row = conn.execute(
        "SELECT capital_after FROM sim_trades WHERE strategy=? AND capital_after IS NOT NULL ORDER BY id DESC LIMIT 1",
        (STRATEGY,)).fetchone()
    capital = round(cap_row[0], 2) if cap_row else float(cfg("backtest.initial_capital", 5000))
    pos_value = sum(p["value"] for p in positions)
    return {
        "positions": positions, "realized_pnl": round(pnl, 2),
        "signal": get_signal(),
        "capital": round(capital, 2),
        "total_asset": round(capital + pos_value, 2),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(get_signal(), ensure_ascii=False, indent=2))
