#!/usr/bin/env python3
"""清除 weekly_eval 残留锁 + 重跑周评估"""
from quant.data.repos._base import DatabaseManager

# 1. 清锁
conn = DatabaseManager.market()
conn.execute(
    "UPDATE task_runs SET status='failed', finished_at=datetime('now') "
    "WHERE task_name='weekly_eval' AND date='2026-08-03' AND status='running'"
)
conn.commit()
conn.close()
print("lock cleared")

# 2. 重跑
from quant.scheduler.weekly import _run
_run('2026-08-03')
