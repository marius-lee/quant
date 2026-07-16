#!/bin/bash
cd "$(dirname "$0")/.."

echo "=== 今日调度任务 ==="
PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
from quant.scheduler.task_log import query_date

tasks = query_date("2026-07-16")
summary = {}
for t in tasks:
    tn = t["task_name"]
    if tn in summary and summary[tn]["status"] == "ok":
        continue
    if t["status"] == "ok":
        summary[tn] = t

for name in ["signals", "execute", "monitor", "daily_data", "attribution"]:
    t = summary.get(name)
    if t:
        s = t.get("summary") or ""
        print(f"  {name:15s} ok     {t['started_at'][:16]}  {s[:60]}")
    else:
        # check latest any status
        latest = None
        for tt in tasks:
            if tt["task_name"] == name:
                latest = tt
                break
        if latest:
            print(f"  {name:15s} {latest['status']:7s} {latest.get('started_at','')[:16]}")
        else:
            print(f"  {name:15s} no record")
PYEOF

echo ""
echo "=== 今日交易 ==="
PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
import sqlite3
c = sqlite3.connect("quant/data/trades.db")
rows = c.execute("SELECT symbol, shares, price, side, cost, created_at FROM sim_trades WHERE date='2026-07-16' ORDER BY created_at").fetchall()
if rows:
    for r in rows:
        print(f"  {r[0]:8s} {r[1]:5d}股 @{r[2]:8.2f} {r[3]:5s} cost={r[4]:.2f}  {r[5]}")
else:
    print("  (无交易)")
c.close()
PYEOF

echo ""
echo "=== 当前持仓 ==="
PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
import sqlite3
c = sqlite3.connect("quant/data/trades.db")
rows = c.execute("""
    SELECT symbol,
           SUM(CASE WHEN side='buy' THEN shares ELSE -shares END) as pos,
           SUM(CASE WHEN side='buy' THEN shares*price + cost ELSE -(shares*price - cost) END) as net_cost
    FROM sim_trades
    GROUP BY symbol HAVING pos > 0
""").fetchall()
if rows:
    total_cost = 0
    for r in rows:
        avg = abs(r[2]) / r[1] if r[1] > 0 else 0
        print(f"  {r[0]:8s} {r[1]:5d}股  成本均价{avg:8.2f}  净成本{r[2]:10.2f}")
        total_cost += abs(r[2])
    print(f"  {'':8s} {'':5s}  持仓总成本{total_cost:14.2f}")
else:
    print("  (无持仓)")
c.close()

# 现金
import sqlite3 as sq
c2 = sq.connect("quant/data/trades.db")
from quant.data.trade_repo import TradeRepo
tr = TradeRepo()
try:
    cash = tr.get_cash()
    print(f"  {'':8s} {'':5s}  可用资金{str(cash):>16s}")
except Exception as e:
    print(f"  cash: ERROR — {e}")
c2.close()
PYEOF
