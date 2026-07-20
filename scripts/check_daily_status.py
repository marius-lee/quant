"""Check daily data status — latest dates and turnover coverage."""
import sys
sys.path.insert(0, '.')
from quant.data.store import market_conn

conn = market_conn()

# 07-17 数据状态
r = conn.execute(
    "SELECT COUNT(*), SUM(CASE WHEN turnover>0 THEN 1 ELSE 0 END) "
    "FROM daily WHERE date='2026-07-17'"
).fetchone()
print(f"07-17: total={r[0]}, with_turnover>0={r[1]}")

# 最新有 turnover 的日期
mr = conn.execute("SELECT MAX(date) FROM daily WHERE turnover>0").fetchone()[0]
print(f"Latest date with turnover>0: {mr}")

# 最近 7 天数据概览
print("\nLast 7 trading days:")
for row in conn.execute(
    "SELECT date, COUNT(*) as cnt, "
    "SUM(CASE WHEN turnover>0 THEN 1 ELSE 0 END) as with_to "
    "FROM daily WHERE date >= '2026-07-10' "
    "GROUP BY date ORDER BY date DESC LIMIT 7"
).fetchall():
    print(f"  {row['date']}: {row['cnt']} stocks, turnover>0={row['with_to']}")

conn.close()
