"""ETF动量轮动策略 — 每周一计算信号, 全仓最优ETF.
来源: 聚宽社区 zfs1 (年化154%, 回撤10%), 适配¥5,000
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


def get_signal() -> dict:
    """计算当前信号: 排名第1的ETF, 附带风控检查."""
    conn = sqlite3.connect(DB)
    scores = []
    today = date.today()

    for code, name, _ in POOL:
        rows = conn.execute(
            "SELECT close FROM daily WHERE symbol=? AND date >= ? ORDER BY date",
            (code, (today - timedelta(days=40)).isoformat())
        ).fetchall()
        if len(rows) < 20:
            continue
        ret_20d = (rows[-1][0] - rows[-20][0]) / rows[-20][0] if rows[-20][0] > 0 else 0
        scores.append((code, name, round(ret_20d, 4)))

    conn.close()

    if not scores:
        return {"action": "hold", "reason": "数据不足"}

    scores.sort(key=lambda x: x[2], reverse=True)
    best = scores[0]

    # 检查溢价率(简化: 跳过, ETF净值需额外数据源)
    # 风控: 全部负收益→空仓国债
    if best[2] <= 0:
        return {"action": "defense", "buy": "511010", "name": "国债ETF",
                "reason": f"全市场负收益(最佳{best[2]:.1%})"}

    return {"action": "buy", "buy": best[0], "name": best[1],
            "score": best[2], "scores": scores}


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
