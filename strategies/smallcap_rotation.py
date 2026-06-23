"""小市值轮动策略 — 流通市值最小Top5等权.
来源: 聚宽社区 (国九小市值年化100%, 回撤25%)
"""
import sqlite3, os
from datetime import date, timedelta
from strategies.base import Strategy

STRATEGY = "smallcap"
DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
EXCLUDE_BOARDS = ("688","8","4","92","900")


class SmallCapStrategy(Strategy):
    STRATEGY = "smallcap"

    def get_signal(self) -> dict:
        conn = sqlite3.connect(DB)
        today = date.today()
        max_date = conn.execute("SELECT MAX(date) FROM daily WHERE date LIKE '%-%-%'").fetchone()[0]
        candidates = []
        for r in conn.execute("""
            SELECT d.symbol, s.name, d.close, d.volume FROM daily d JOIN stocks s ON d.symbol=s.symbol
            WHERE d.date=? AND d.close>0 AND d.volume>0 AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
              AND d.symbol NOT LIKE '688%' AND d.symbol NOT LIKE '8%' AND d.symbol NOT LIKE '4%' AND d.symbol NOT LIKE '92%'
        """,(max_date,)).fetchall():
            prev = conn.execute("SELECT close FROM daily WHERE symbol=? AND date < ? ORDER BY date DESC LIMIT 1",(r[0],max_date)).fetchone()
            if prev and prev[0]>0 and (r[2]/prev[0]-1)>=0.095: continue
            candidates.append({"symbol":r[0],"name":r[1],"close":r[2],"volume":r[3]})
        conn.close()
        candidates.sort(key=lambda x: x["volume"]*x["close"])
        picks = [{"symbol":s["symbol"],"name":s["name"],"close":s["close"]} for s in candidates if s["close"]<=50]
        if today.month in (1,4): return {"action":"defense","reason":f"{today.month}月财报季空仓"}
        return {"action":"rotate","picks":picks[:20],"count":len(picks)}


_inst = SmallCapStrategy()
get_signal = _inst.get_signal
record_trade = _inst.record_trade
get_state = _inst.get_state
