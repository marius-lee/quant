#!/usr/bin/env python3
"""Diagnose state_broker signal loading failure."""
import os, sys, sqlite3, json

# Replicate state_broker logic
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
print(f"_root: {_root}")

sig_path = os.path.join(_root, "quant", "data", "trades.db")
print(f"sig_path: {sig_path}")
print(f"exists: {os.path.exists(sig_path)}")

sc = sqlite3.connect(sig_path)
sc.row_factory = sqlite3.Row

from datetime import datetime
today = datetime.now().strftime("%Y-%m-%d")
print(f"today: {today}")

# Check mode column
cols = [r[1] for r in sc.execute("PRAGMA table_info(daily_signals)").fetchall()]
print(f"daily_signals columns: {cols}")

# Raw query
row = sc.execute(
    "SELECT * FROM daily_signals WHERE date=? ORDER BY generated_at DESC LIMIT 1",
    (today,)
).fetchone()
if row:
    print(f"row keys: {list(row.keys())}")
    print(f"signals_json: {row['signals_json'][:80] if row['signals_json'] else 'NULL'}...")
    print(f"mode: {row.get('mode', 'NO MODE FIELD')}")
else:
    print("NO ROW FOR TODAY")

# Try with mode filter
row2 = sc.execute(
    "SELECT * FROM daily_signals WHERE date=? AND mode='live' ORDER BY generated_at DESC LIMIT 1",
    (today,)
).fetchone()
print(f"with mode='live': {'FOUND' if row2 else 'NOT FOUND'}")

sc.close()
