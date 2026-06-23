"""大盘择时策略 — 沪深300趋势判断, 涨则买最强龙头股.
来源: 聚宽天梯 脆脆鲨l (年化107.87%, 回撤21.65%)
"""
import sqlite3, os
from datetime import date, timedelta
from strategies.base import Strategy

STRATEGY = "timing"
DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
INDEX_SYMBOL = "510300"
THRESHOLD = 0.02


class TimingStrategy(Strategy):
    STRATEGY = "timing"

    def _pick_leader(self) -> tuple:
        conn = sqlite3.connect(DB)
        rows = conn.execute("""
            SELECT symbol FROM daily WHERE date=(SELECT MAX(date) FROM daily WHERE date LIKE '%-%-%')
              AND symbol NOT LIKE '688%' AND close>0 AND volume>0 ORDER BY close*volume DESC LIMIT 300
        """).fetchall()
        candidates = []
        for (sym,) in rows:
            name_row = conn.execute("SELECT name FROM stocks WHERE symbol=?",(sym,)).fetchone()
            if name_row and ('ST' in name_row[0] or '退' in name_row[0]): continue
            prices = conn.execute("SELECT close FROM daily WHERE symbol=? AND date >= ? ORDER BY date",
                (sym,(date.today()-timedelta(days=40)).isoformat())).fetchall()
            if len(prices)>=20 and prices[-20][0]>0 and prices[-1][0]<=50:
                candidates.append((sym, (prices[-1][0]-prices[-20][0])/prices[-20][0], prices[-1][0]))
        conn.close()
        return max(candidates, key=lambda x:x[1]) if candidates else ("","",0)

    def get_signal(self) -> dict:
        conn = sqlite3.connect(DB)
        rows = conn.execute("SELECT close FROM daily WHERE symbol=? AND date >= ? ORDER BY date",
            (INDEX_SYMBOL,(date.today()-timedelta(days=40)).isoformat())).fetchall()
        conn.close()
        if len(rows)<20: return {"action":"hold","reason":"数据不足"}
        ret_20d = (rows[-1][0]-rows[-20][0])/rows[-20][0] if rows[-20][0]>0 else 0
        if ret_20d>=THRESHOLD:
            sym,ret,px = self._pick_leader()
            if sym: return {"action":"buy","symbol":sym,"name":"","price":round(px,2),"ret_20d":round(ret_20d,4),"leader_ret":round(ret,4)}
        return {"action":"sell","reason":f"20日涨幅{ret_20d:.1%}<{THRESHOLD:.0%}"}

    def execute(self) -> bool:
        sig = self.get_signal()
        mc = sqlite3.connect(DB)
        tc = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(__file__)),"data","trades.db"))
        pos = self._get_positions()
        if sig["action"]=="buy":
            sym = sig["symbol"]
            if pos and pos[0][0]==sym: mc.close();tc.close(); return True
            for r in pos: self.record_trade(r[0],r[1],r[2],"sell")
            px = sig.get("price",0)
            if px>0:
                cap = self._capital()
                lots = self.affordable_lots(cap, px)
                if lots>=1: self.record_trade(sym, px, lots*100, "buy")
        elif sig["action"]=="sell" and pos:
            for r in pos:
                row = mc.execute("SELECT close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1",(r[0],)).fetchone()
                if row: self.record_trade(r[0], row[0], r[2], "sell")
        mc.close();tc.close()
        return True


_inst = TimingStrategy()
get_signal = _inst.get_signal
record_trade = _inst.record_trade
get_state = _inst.get_state
execute = _inst.execute
