"""检查换手率回填结果"""
from quant.data.store import DataStore
s = DataStore()
conn = s._connect()
r = conn.execute(
    "SELECT date, COUNT(*) as n, SUM(CASE WHEN turnover>0 THEN 1 ELSE 0 END) as with_to "
    "FROM daily WHERE date >= '2026-07-10' GROUP BY date ORDER BY date"
).fetchall()
for row in r:
    print(f"{row[0]}: total={row[1]}, turnover>0={row[2]}")
conn.close()
