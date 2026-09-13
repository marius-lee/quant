"""v625: reset stuck task_runs rows left by the pre-v625 scheduling bugs.

Targets (verified against quant/data/market.db before fix):
  - signals 2026-09-08 'running' (pid=51902 = web/app.py InlineRunner rerun;
    orchestrator daemon was not running, _check_timeouts never fired -> row
    stuck 'running' since 08:30).  [Bug B1 / HANDOFF v609]
  - monitor 'lunch' x3 (2026-08-07 / 08-13 / 08-14): _cleanup_zombie_tasks only
    reclaimed 'running', not 'lunch', so midday-killed monitor rows were
    permanent.  [Bug B7 / HANDOFF v625]

Resetting to 'aborted' lets the scheduler re-trigger on the next window instead
of displaying a forever-running task. Follows the precedent of
scripts/cleanup_stuck_tasks.py.  Uses the single-source MARKET_DB path.
"""
import sqlite3
from quant.config.paths import MARKET_DB

# (task_name, date) rows observed stuck on the live schedule page
STUCK = [
    ("signals", "2026-09-08"),
    ("monitor", "2026-08-07"),
    ("monitor", "2026-08-13"),
    ("monitor", "2026-08-14"),
]

conn = sqlite3.connect(MARKET_DB)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=5000")
total = 0
for name, d in STUCK:
    n = conn.execute(
        "UPDATE task_runs SET status='aborted', "
        "finished_at=datetime('now','localtime'), "
        "error='v625 manual reset: stuck row (daemon-not-running / lunch-zombie)' "
        "WHERE task_name=? AND date=? AND status IN ('running','lunch')",
        (name, d),
    ).rowcount
    total += n
    print(f"  {name} {d}: reset {n} row(s)")
conn.commit()
conn.close()
print(f"v625 reset complete: {total} row(s) total")
