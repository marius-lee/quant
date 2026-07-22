"""手动补跑 signals — 清理僵尸 row 后重新生成今日信号"""
import sqlite3
import uuid

# 1. 清理僵尸 running 行
db = sqlite3.connect('quant/data/market.db')
db.execute("UPDATE task_runs SET status='aborted', finished_at=datetime('now'), error='pre-regen cleanup' WHERE date='2026-07-22' AND status='running'")
db.commit()
print(f"cleaned {db.total_changes} zombie rows")
db.close()

# 2. 重新生成信号
from quant.utils.logger import get_logger, set_trace_id
tid = uuid.uuid4().hex[:12]
set_trace_id(tid)
_log = get_logger("manual_signals")
_log.info(f"manual signals regen trace_id={tid}")

from quant.pipeline import generate_signals
result = generate_signals(date_str='2026-07-22', skip_pull=True)
targets = result.get("target_positions", [])
print(f"done: {len(targets)} targets")
for t in targets:
    print(f"  {t['symbol']} {t['side']} shares={t['shares']} price≈{t['price']}")
