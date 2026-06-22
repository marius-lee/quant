"""ETF动量轮动策略 — R²拟合优度加权, 每周一计算信号.
来源: 聚宽社区 zfs1 (年化154%) + BigQuant 趋势稳健性动量 (夏普1.06)
增强: 年化收益率 × R² 双因子评分, 过滤假趋势
"""
import sqlite3, os
from datetime import date, timedelta

STRATEGY = "etf"
DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")

# ETF池 (代码, 名称, 类型)
POOL = [
    ("510300", "沪深300ETF", "大盘"),
    ("510500", "中证500ETF", "中盘"),
    ("159915", "创业板ETF", "成长"),
    ("510880", "红利ETF", "价值"),
    ("511010", "国债ETF", "防御"),
]


def _r_squared(prices: list) -> float:
    """线性回归R² — 衡量趋势稳定性 (来源: BigQuant, >0.5有效)."""
    n = len(prices)
    if n < 5:
        return 0
    x = list(range(n))
    x_mean = sum(x) / n
    y_mean = sum(prices) / n
    ss_xy = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, prices))
    ss_xx = sum((xi - x_mean) ** 2 for xi in x)
    ss_yy = sum((yi - y_mean) ** 2 for yi in prices)
    if ss_xx == 0 or ss_yy == 0:
        return 0
    r = ss_xy / (ss_xx ** 0.5 * ss_yy ** 0.5)
    return r ** 2


def get_signal() -> dict:
    """R²加权评分: 年化收益率 × R², 过滤R²<0.3的假趋势."""
    conn = sqlite3.connect(DB)
    scores = []
    today = date.today()

    for code, name, _ in POOL:
        rows = conn.execute(
            "SELECT close FROM daily WHERE symbol=? AND date >= ? ORDER BY date",
            (code, (today - timedelta(days=60)).isoformat())
        ).fetchall()
        if len(rows) < 30:
            continue
        prices = [r[0] for r in rows]
        ret_30d = (prices[-1] - prices[0]) / prices[0] if prices[0] > 0 else 0
        annual_ret = ret_30d * (252 / 30)  # 年化
        r2 = _r_squared(prices[-30:])  # 最近30日R²
        score = round(annual_ret * r2, 4)
        scores.append((code, name, score, round(annual_ret, 3), round(r2, 3)))

    conn.close()

    if not scores:
        return {"action": "hold", "reason": "数据不足",
                "annual_ret": 0, "r2": 0, "score": 0, "scores": []}

    scores.sort(key=lambda x: x[2], reverse=True)
    best = scores[0]
    score_list = [(s[0], s[1], s[2], s[3], s[4]) for s in scores]

    if best[2] <= 0 or best[3] < 0:
        return {"action": "defense", "buy": "511010", "name": "国债ETF",
                "reason": "全市场无正收益趋势",
                "annual_ret": best[3], "r2": best[4], "score": best[2],
                "scores": score_list}

    return {"action": "buy", "buy": best[0], "name": best[1],
            "score": best[2], "annual_ret": best[3], "r2": best[4],
            "scores": score_list}


def record_trade(symbol, name, price, shares, side="buy"):
    """记录交易到 sim_trades."""
    conn = sqlite3.connect(TRADE_DB)
    cost = price * shares
    fee = max(cost * 0.0003, 5)
    pnl = None
    capital_after = None
    if side == "sell":
        # 计算PnL
        buy = conn.execute(
            "SELECT price, shares FROM sim_trades WHERE symbol=? AND side='buy' AND strategy=? ORDER BY id DESC LIMIT 1",
            (symbol, STRATEGY)
        ).fetchone()
        if buy:
            pnl = round((price - buy[0]) * shares - fee - max(buy[0] * buy[1] * 0.0003, 5), 2)
        capital_after = 0
    conn.execute(
        "INSERT INTO sim_trades (date, symbol, side, price, shares, strategy) VALUES (?,?,?,?,?,?)",
        (date.today().isoformat(), symbol, side, price, shares, STRATEGY)
    )
    conn.commit()
    conn.close()
    return pnl


def _get_realtime(symbols: list) -> dict:
    """获取实时价格 (Sina API)."""
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

    # 实时价格 + 名称
    name_map = {c: n for c, n, _ in POOL}
    symbols = [r[0] for r in pos]
    quotes = _get_realtime(symbols) if symbols else {}
    positions = []
    for r in pos:
        sym, cost, shares = r[0], r[1], r[2]
        q = quotes.get(sym, {})
        current = q.get("price", cost) if q else cost
        name = q.get("name", "") or name_map.get(sym, "")
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
    sig = get_signal()
    return {
        "positions": positions, "realized_pnl": round(pnl, 2),
        "signal": sig,
        "capital": round(capital, 2),
        "total_asset": round(capital + pos_value, 2),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(get_signal(), ensure_ascii=False, indent=2))
