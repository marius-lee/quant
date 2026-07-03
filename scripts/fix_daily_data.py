"""P0-1: 删除未复权日线 + 重拉 qfq 数据。

步骤:
  1. 删除 2025-06-01 之后的日线 (Sina 未复权污染)
  2. VACUUM 回收磁盘
  3. 用 tencent/akshare/pytdx (全部 qfq) 重拉
"""

import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
CUTOFF = "2025-06-01"

# Step 1: Delete
conn = sqlite3.connect(DB)
before = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
to_del = conn.execute("SELECT COUNT(*) FROM daily WHERE date >= ?", (CUTOFF,)).fetchone()[0]
print(f"Before: {before:,} rows")
print(f"Deleting: {to_del:,} rows (from {CUTOFF})")
conn.execute("DELETE FROM daily WHERE date >= ?", (CUTOFF,))
conn.commit()

# Step 2: VACUUM
print("VACUUM...")
conn.execute("VACUUM")
after = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
print(f"After: {after:,} rows")
conn.close()

# Step 3: Repull
print("\nRepulling with qfq sources (tencent/akshare/pytdx)...")
from data.store import DataStore
s = DataStore()
n = s.update_daily(start=CUTOFF)
print(f"\nDone: {n} new rows pulled")

# Verify
conn = sqlite3.connect(DB)
final = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
dates = conn.execute("SELECT MIN(date), MAX(date) FROM daily").fetchone()
# Check if 688167 still has the -35% jump
rows = conn.execute(
    "SELECT date, close FROM daily WHERE symbol='688167' AND date >= '2026-06-01' ORDER BY date"
).fetchall()
if rows:
    print(f"\n688167 verification (should have no >20% daily drop):")
    for i in range(1, len(rows)):
        drop = (rows[i][1] - rows[i-1][1]) / rows[i-1][1] if rows[i-1][1] > 0 else 0
        if abs(drop) > 0.15:
            print(f"  WARN: {rows[i-1][0]} -> {rows[i][0]}: {drop*100:.1f}%")

conn.close()
print(f"\nDate range: {dates[0]} -> {dates[1]}, {final:,} total rows")
print("P0-1 complete.")
