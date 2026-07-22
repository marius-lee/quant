#!/usr/bin/env python3
import sqlite3, json

conn = sqlite3.connect("quant/data/trades.db")
row = conn.execute(
    "SELECT date, signals_json, exec_notes FROM daily_signals ORDER BY date DESC LIMIT 1"
).fetchone()

if row:
    print(f"latest date: {row[0]}")
    sigs = json.loads(row[1]) if row[1] else []
    print(f"signals count: {len(sigs)}")
    for s in sigs[:3]:
        print(json.dumps(s, ensure_ascii=False, indent=2))
    print(f"exec_notes: {row[2]}")
else:
    print("daily_signals: no rows")
conn.close()
