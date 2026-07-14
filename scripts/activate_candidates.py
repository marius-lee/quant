#!/usr/bin/env python3
"""P43: 激活 bp_ratio / size / gap_5d 候选因子."""
import sqlite3
from quant.data.repos._base import DatabaseManager, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

db = sqlite3.connect('data/market.db')
for name in ['bp_ratio', 'size', 'gap_5d']:
    db.execute(
        "UPDATE factor_registry SET status='active', status_reason='P43 sleeve candidate (fundamentals + gap, low corr with zt_streak)',"
        " updated_at=datetime('now','localtime') WHERE name=?",
        (name,)
    )
db.commit()
rows = db.execute("SELECT name, status FROM factor_registry WHERE status='active'").fetchall()
for r in rows:
    print(f"  {r[0]:30s} {r[1]}")
print(f"\n{len(rows)} active factors total")
db.close()
