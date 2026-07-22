#!/usr/bin/env python3
"""检查 execute 补跑状态和当前挂单。"""
import sqlite3

mdb = sqlite3.connect("quant/data/market.db")
tdb = sqlite3.connect("quant/data/trades.db")

print("=== execute 补跑情况 ===")
for r in mdb.execute(
    "SELECT task_name,status,started_at,summary FROM task_runs "
    "WHERE date='2026-07-22' AND task_name='execute' ORDER BY started_at DESC LIMIT 3"
).fetchall():
    print(f"  {r[0]} {r[1]} {r[2]} summary={r[3]}")

print("\n=== 当前挂单 (pending) ===")
rows = tdb.execute(
    "SELECT symbol,status,target_shares,limit_price,placed_at,cancel_reason "
    "FROM pending_orders WHERE day='2026-07-22' AND status='pending' ORDER BY placed_at DESC"
).fetchall()
print(f"  rows: {len(rows)}")
for r in rows:
    print(f"  {r[0]} {r[1]} {r[2]}股 limit=¥{r[3]} at {r[4]} reason={r[5]}")

mdb.close()
tdb.close()
