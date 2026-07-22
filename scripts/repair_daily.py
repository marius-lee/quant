"""一次性修复: 清理僵尸 task + 拉 07-21 日线 + 补 turnover 缺口."""
import sqlite3
import sys
sys.path.insert(0, ".")

# ── Step 1: 清理僵尸 task_runs ──
conn = sqlite3.connect("quant/data/market.db")
conn.execute("""UPDATE task_runs SET status='aborted',
    finished_at=datetime('now'), error='manual cleanup'
    WHERE task_name='daily_data' AND date='2026-07-21' AND status='running'""")
conn.commit()
conn.close()
print("Step 1: zombie cleaned")

# ── Step 2: 拉 07-21 日线 ──
from quant.data.store import DataStore
s = DataStore()
s.update_daily("2026-07-21")
s.close()
print("Step 2: daily 07-21 pulled")

# ── Step 3: 补 turnover 缺口 (只查 turnover=0 的, 不重复拉) ──
s2 = DataStore()
n = s2.backfill_turnover()
s2.close()
print(f"Step 3: backfill done, {n} updated")
