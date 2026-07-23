#!/bin/bash
# Clean zombie signals task + rerun
set -e
cd "$(dirname "$0")/.."
DATE="${1:-$(date +%Y-%m-%d)}"

echo "=== clean zombie signals for $DATE ==="
PYTHONPATH=. .venv/bin/python3 -c "
import sqlite3, sys
mdb = sqlite3.connect('quant/data/market.db')
mdb.execute(\"UPDATE task_runs SET status='aborted', finished_at=datetime('now','localtime'), error='manual cleanup' WHERE task_name='signals' AND date=? AND status='running'\", (sys.argv[1],))
mdb.commit()
print(f'cleaned {mdb.total_changes} zombie')
mdb.close()
" "$DATE"

echo "=== rerun signals for $DATE ==="
bash scripts/run_task.sh signals "$DATE"
