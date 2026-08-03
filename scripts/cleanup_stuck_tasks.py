"""清理今天卡住的 running 行 — 进程已死但 _tk_finish 未执行."""
import sqlite3
db = '/Users/mariusto/project/quant/quant/data/market.db'
conn = sqlite3.connect(db)
conn.execute('PRAGMA journal_mode=WAL')
n = conn.execute(
    "UPDATE task_runs SET status='failed', finished_at=datetime('now','localtime'), "
    "error='进程崩溃(未正常结束)' WHERE date='2026-08-03' AND status='running'"
).rowcount
conn.commit()
conn.close()
print(f'cleaned {n} stuck rows')
