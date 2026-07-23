"""清理僵尸归因任务 + 补跑归因"""
import sqlite3, sys

date = sys.argv[1] if len(sys.argv) > 1 else '2026-07-22'

mdb = sqlite3.connect('quant/data/market.db')
mdb.execute(f"UPDATE task_runs SET status='aborted', finished_at=datetime('now','localtime'), error='manual cleanup' WHERE task_name='attribution' AND date='{date}' AND status='running'")
mdb.commit()
n = mdb.total_changes
mdb.close()
print(f'cleaned {n} zombie rows for {date}')

if n == 0:
    print('no zombies, proceeding anyway')

from quant.scheduler.attribution import _run
_run(date)
