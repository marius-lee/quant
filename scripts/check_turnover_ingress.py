"""检查 turnover 回填是否真的入了库."""
import sqlite3, os

db = os.path.expanduser("/Users/mariusto/project/quant/quant/data/market.db")
conn = sqlite3.connect(db)

# 检查 07-10 的 turnover 更新情况
print("=== 2026-07-10 turnover 分布 ===")
r = conn.execute("""
    SELECT 
        COUNT(*) as total,
        SUM(CASE WHEN turnover > 0 THEN 1 ELSE 0 END) as has_turnover,
        SUM(CASE WHEN turnover = 0 OR turnover IS NULL THEN 1 ELSE 0 END) as missing
    FROM daily WHERE date = '2026-07-10'
""").fetchone()
print(f"  total={r[0]}  turnover>0={r[1]}  missing={r[2]}")

# 抽样 10 只有 turnover 的
print("\n=== 07-10 turnover>0 抽样 (10 rows) ===")
for row in conn.execute("""
    SELECT symbol, turnover FROM daily 
    WHERE date='2026-07-10' AND turnover > 0
    LIMIT 10
""").fetchall():
    print(f"  {row[0]}: {row[1]:.4f}")

# 检查所有日期
print("\n=== 缺口日期 turnover 统计 ===")
for date in ['2026-07-10','2026-07-13','2026-07-14','2026-07-15','2026-07-16','2026-07-17','2026-07-20']:
    r = conn.execute("""
        SELECT COUNT(*), SUM(CASE WHEN turnover>0 THEN 1 ELSE 0 END)
        FROM daily WHERE date=?
    """, (date,)).fetchone()
    print(f"  {date}: total={r[0]}  turnover>0={r[1]}")

conn.close()
