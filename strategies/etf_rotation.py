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
    ("513100", "纳指ETF", "海外科技"),
    ("159915", "创业板ETF", "A股成长"),
    ("518880", "黄金ETF", "避险"),
    ("511010", "国债ETF", "防御"),
    ("510880", "红利ETF", "价值"),
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
        if r2 >= 0.3:  # 过滤假趋势
            score = round(annual_ret * r2, 4)
            scores.append((code, name, score, round(annual_ret, 3), round(r2, 3)))

    conn.close()

    if not scores:
        return {"action": "hold", "reason": "无有效趋势(R²均<0.3)"}

    scores.sort(key=lambda x: x[2], reverse=True)
    best = scores[0]

    if best[2] <= 0:
        return {"action": "defense", "buy": "511010", "name": "国债ETF",
                "reason": f"全市场负评分(最佳{best[2]:.3f})"}

    return {"action": "buy", "buy": best[0], "name": best[1],
            "score": best[2], "annual_ret": best[3], "r2": best[4],
            "scores": [(s[0], s[1], s[2], s[3], s[4]) for s in scores]}


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


def get_state() -> dict:
    """返回当前策略状态."""
    conn = sqlite3.connect(TRADE_DB)
    # 当前持仓
    pos = conn.execute(
        "SELECT symbol, price, shares FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
        (STRATEGY, STRATEGY)
    ).fetchall()
    # 已实现盈亏
    pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM sim_trades WHERE side='sell' AND strategy=?",
        (STRATEGY,)
    ).fetchone()[0]
    conn.close()

    sig = get_signal()
    return {
        "positions": [{"symbol": r[0], "price": r[1], "shares": r[2]} for r in pos],
        "realized_pnl": round(pnl, 2),
        "signal": sig,
        "capital": 5000.0 + pnl - sum(r[1] * r[2] for r in pos),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(get_signal(), ensure_ascii=False, indent=2))
