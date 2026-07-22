"""检查今日挂单和成交状态"""
import sqlite3, json

tdb = sqlite3.connect('quant/data/trades.db')
tdb.row_factory = sqlite3.Row

def d(r):
    return dict(r)

print("=== Pending Orders ===")
rows = tdb.execute(
    "SELECT * FROM pending_orders WHERE day='2026-07-22' ORDER BY placed_at DESC LIMIT 5"
).fetchall()
if not rows:
    print("  (empty)")
for r in rows:
    rd = d(r)
    print(f"  {rd['symbol']} {rd['side']} target={rd['target_shares']} "
          f"filled={rd['filled_shares']}@{(rd.get('filled_price') or '?')} "
          f"status={rd['status']} cancel={rd.get('cancel_reason','')}")

print("\n=== Today's Trades ===")
rows = tdb.execute(
    "SELECT * FROM sim_trades WHERE date='2026-07-22' ORDER BY created_at DESC LIMIT 5"
).fetchall()
if not rows:
    print("  (empty)")
for r in rows:
    rd = d(r)
    print(f"  {rd['symbol']} {rd['side']} shares={rd['shares']} price={rd['price']} cost={rd['cost']}")

print("\n=== Daily Signals ===")
rows = tdb.execute(
    "SELECT generated_at, signals_json, exec_notes FROM daily_signals "
    "WHERE date='2026-07-22' ORDER BY generated_at DESC LIMIT 1"
).fetchall()
for r in rows:
    rd = d(r)
    sigs = json.loads(rd['signals_json'])
    print(f"  generated={rd['generated_at']}")
    print(f"  exec_notes={rd['exec_notes']}")
    for s in sigs:
        print(f"    {s['symbol']} {s['side']} shares={s['shares']} score={s.get('score','?')}")

tdb.close()
