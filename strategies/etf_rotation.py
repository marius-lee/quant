"""ETF动量轮动策略 — R²拟合优度加权.
来源: 聚宽社区 zfs1 (年化154%) + BigQuant 趋势稳健性动量 (夏普1.06)
"""
import sqlite3, os
from datetime import date, timedelta
from strategies.base import Strategy

STRATEGY = "etf"
DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")

POOL = [
    ("510300", "沪深300ETF", "大盘"), ("510500", "中证500ETF", "中盘"),
    ("159915", "创业板ETF", "成长"), ("510880", "红利ETF", "价值"),
    ("511010", "国债ETF", "防御"),
]


def _r_squared(prices: list) -> float:
    n = len(prices)
    if n < 5: return 0
    x = list(range(n))
    xm, ym = sum(x)/n, sum(prices)/n
    sxy = sum((xi-xm)*(yi-ym) for xi,yi in zip(x,prices))
    sxx = sum((xi-xm)**2 for xi in x)
    syy = sum((yi-ym)**2 for yi in prices)
    return (sxy/(sxx**0.5*syy**0.5))**2 if sxx>0 and syy>0 else 0


class ETFStrategy(Strategy):
    STRATEGY = "etf"

    def get_signal(self) -> dict:
        conn = sqlite3.connect(DB)
        scores = []
        for code, name, _ in POOL:
            rows = conn.execute(
                "SELECT close FROM daily WHERE symbol=? AND date >= ? ORDER BY date",
                (code, (date.today() - timedelta(days=60)).isoformat())).fetchall()
            if len(rows) < 30: continue
            prices = [r[0] for r in rows]
            ret_30d = (prices[-1]-prices[0])/prices[0] if prices[0]>0 else 0
            r2 = _r_squared(prices[-30:])
            scores.append((code, name, round(ret_30d*(252/30)*r2,4), round(ret_30d*(252/30),3), round(r2,3)))
        conn.close()
        if not scores:
            return {"action":"hold","reason":"数据不足","annual_ret":0,"r2":0,"score":0,"scores":[]}
        scores.sort(key=lambda x:x[2], reverse=True)
        best = scores[0]; sl = [(s[0],s[1],s[2],s[3],s[4]) for s in scores]
        if best[2]<=0 or best[3]<0:
            return {"action":"defense","buy":"511010","name":"国债ETF","reason":"全市场无正收益趋势","annual_ret":best[3],"r2":best[4],"score":best[2],"scores":sl}
        return {"action":"buy","buy":best[0],"name":best[1],"score":best[2],"annual_ret":best[3],"r2":best[4],"scores":sl}

    def get_state(self) -> dict:
        """覆写: ETF需要名称映射."""
        state = super().get_state()
        name_map = {c:n for c,n,_ in POOL}
        for p in state["positions"]:
            if not p["name"]:
                p["name"] = name_map.get(p["symbol"], "")
        return state


_inst = ETFStrategy()
get_signal = _inst.get_signal
record_trade = _inst.record_trade
get_state = _inst.get_state
