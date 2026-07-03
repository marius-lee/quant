"""P0-1: 删除未复权日线 + 重拉 qfq 数据。

进度: 每 50 只输出一行 (已完成/总数 + 百分比)
"""

import sqlite3, sys, os, time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
CUTOFF = "2025-06-01"

# ── Step 1: Delete ──
conn = sqlite3.connect(DB)
before = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
to_del = conn.execute("SELECT COUNT(*) FROM daily WHERE date >= ?", (CUTOFF,)).fetchone()[0]
print(f"Before: {before:,} rows")
print(f"Deleting: {to_del:,} rows (from {CUTOFF})")
conn.execute("DELETE FROM daily WHERE date >= ?", (CUTOFF,))
conn.commit()

print("VACUUM...")
conn.execute("VACUUM")
after = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
print(f"After: {after:,} rows\n")

# ── 统计待拉取股票数 ──
# 只统计非 BJ 股票
total_symbols = conn.execute(
    "SELECT COUNT(*) FROM stocks WHERE market!='BJ'"
).fetchone()[0]
conn.close()

# ── Step 2: Repull with progress ──
print(f"Repulling qfq data for {total_symbols} stocks (tencent/akshare/pytdx)...")
print(f"Progress: 每 50 只一行, ~{total_symbols//50} 批\n")

from data.store import DataStore
s = DataStore()

# 分批调用 update_daily, 手动控制进度输出
batch_size = 50
symbols = sorted([r[0] for r in s._connect().execute(
    "SELECT symbol FROM stocks WHERE market!='BJ'").fetchall()],
    key=lambda x: x[:2])  # SH first

t_start = time.time()
total_new = 0
batch_count = 0
source_counts = {}

for i in range(0, len(symbols), batch_size):
    chunk = symbols[i:i + batch_size]
    n = s.update_daily(chunk, start=CUTOFF)
    total_new += n
    batch_count += 1
    done = min(i + batch_size, len(symbols))
    pct = done / len(symbols) * 100
    elapsed = time.time() - t_start
    rate = done / elapsed if elapsed > 0 else 0
    eta = (len(symbols) - done) / rate if rate > 0 else 0
    print(f"  [{done}/{len(symbols)} {pct:.0f}%] "
          f"{total_new} new rows | {rate:.0f} stocks/s | ETA {eta:.0f}s",
          flush=True)

elapsed = time.time() - t_start
print(f"\nDone: {total_new} new rows in {elapsed:.0f}s ({len(symbols)/elapsed:.0f} stocks/s)")

# ── Verify ──
conn = sqlite3.connect(DB)
final = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
dates = conn.execute("SELECT MIN(date), MAX(date) FROM daily").fetchone()
print(f"Date range: {dates[0]} -> {dates[1]}, {final:,} total rows")

# 检查 688167 是否还有除权跳空
rows = conn.execute(
    "SELECT date, close FROM daily WHERE symbol='688167' AND date >= '2026-06-01' ORDER BY date"
).fetchall()
if rows:
    print("\n688167 check (should have no >20% daily drop):")
    found_bad = False
    for i in range(1, len(rows)):
        if rows[i-1][1] > 0:
            drop = (rows[i][1] - rows[i-1][1]) / rows[i-1][1]
            if abs(drop) > 0.15:
                print(f"  FAIL: {rows[i-1][0]} -> {rows[i][0]}: {drop*100:.1f}%")
                found_bad = True
    if not found_bad:
        print("  OK: no extreme jumps")

conn.close()
print("\nP0-1 complete.")
