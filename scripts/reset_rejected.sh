#!/bin/bash
# === 安全重置 rejected → retired ===
# 只重置 Phase 2/3/4 评估失败的因子，保护永久 rejected（数据源已死等）。
# 用法:
#   PYTHONPATH=. bash scripts/reset_rejected.sh          # 预览
#   PYTHONPATH=. bash scripts/reset_rejected.sh --apply  # 执行重置
set -e
cd "$(dirname "$0")/.."

if [ "${1:-}" = "--apply" ]; then
    echo ">>> RESET: rejected → retired (仅 Phase 评估失败因子)"
    .venv/bin/python3 -c "
from utils.excepthook import setup; setup()
import sqlite3
conn = sqlite3.connect('data/market.db')
rows = conn.execute('''
    SELECT name, status_reason FROM factor_registry
    WHERE status='rejected'
      AND status_reason LIKE 'Phase %: %'
''').fetchall()
if not rows:
    print('没有可重置的 rejected 因子')
else:
    for name, reason in rows:
        conn.execute('''
            UPDATE factor_registry SET status='retired',
            status_reason='reset from rejected (Phase eval retry)',
            updated_at=datetime('now','localtime')
            WHERE name=?
        ''', (name,))
        print(f'  {name} → retired  (was: {reason})')
    conn.commit()
    print(f'Done: {len(rows)} factors reset to retired')
skipped = conn.execute('''
    SELECT name, status_reason FROM factor_registry
    WHERE status='rejected'
      AND status_reason NOT LIKE 'Phase %: %'
''').fetchall()
if skipped:
    print(f'Skipped {len(skipped)} (permanent rejection):')
    for name, reason in skipped:
        print(f'  SKIP {name}: {reason}')
conn.close()
"
else
    echo ">>> PREVIEW: 即将重置的 rejected 因子 (用 --apply 执行)"
    .venv/bin/python3 -c "
from utils.excepthook import setup; setup()
import sqlite3
conn = sqlite3.connect('data/market.db')
rows = conn.execute('''
    SELECT name, status_reason FROM factor_registry
    WHERE status='rejected'
      AND status_reason LIKE 'Phase %: %'
''').fetchall()
print(f'可重置: {len(rows)}')
for name, reason in rows:
    print(f'  {name}: {reason}')
skipped = conn.execute('''
    SELECT name, status_reason FROM factor_registry
    WHERE status='rejected'
      AND status_reason NOT LIKE 'Phase %: %'
''').fetchall()
if skipped:
    print(f'永久 rejected (跳过): {len(skipped)}')
    for name, reason in skipped:
        print(f'  SKIP {name}: {reason}')
conn.close()
"
fi
