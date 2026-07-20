"""检查换手率回填进度"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from quant.data.store import DataStore
s = DataStore()
conn = s._connect()
n21 = conn.execute("SELECT COUNT(*) FROM daily WHERE date='2026-07-21'").fetchone()[0]
print(f"daily rows for 07-21: {n21}")
r = conn.execute(
    "SELECT date, COUNT(*) FROM daily WHERE turnover>0 "
    "GROUP BY date ORDER BY date DESC LIMIT 5"
).fetchall()
for d, c in r:
    print(f"  {d}: {c} stocks with turnover>0")
s.close()
