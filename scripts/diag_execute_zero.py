#!/usr/bin/env python3
"""排查 execute limit_buys=0 的原因。"""
import sqlite3, json

tdb = sqlite3.connect("quant/data/trades.db")
tdb.row_factory = sqlite3.Row

# 1. all signals for today
print("=== 今日信号 (所有版本) ===")
for r in tdb.execute(
    "SELECT date, mode, generated_at, signals_json, exec_notes "
    "FROM daily_signals WHERE date='2026-07-22' ORDER BY generated_at DESC"
).fetchall():
    sigs = json.loads(r["signals_json"]) if r["signals_json"] else []
    print(f"  generated={r['generated_at']} mode={r['mode']} sigs={len(sigs)}")
    print(f"  exec_notes={r['exec_notes']}")
    for s in sigs:
        print(f"    {s['symbol']} {s['side']} {s['shares']}股 price≈{s['price']}")

# 2. all pending orders today
print("\n=== 今日所有挂单 (含cancelled) ===")
rows = tdb.execute(
    "SELECT symbol, status, target_shares, limit_price, placed_at, cancel_reason "
    "FROM pending_orders WHERE day='2026-07-22' ORDER BY placed_at"
).fetchall()
print(f"  rows: {len(rows)}")
for r in rows:
    print(f"  {r['symbol']} {r['status']} {r['target_shares']}股 limit=¥{r['limit_price']} cancel={r['cancel_reason']}")

# 3. sim_trades today
print("\n=== 今日成交 ===")
rows = tdb.execute(
    "SELECT * FROM sim_trades WHERE date='2026-07-22'"
).fetchall()
print(f"  rows: {len(rows)}")
for r in rows:
    print(f"  {dict(r)}")

tdb.close()

# 4. latest signal in TradeRepo format
print("\n=== TradeRepo.get_latest_signals() ===")
import sys; sys.path.insert(0, ".")
from quant.data.repos import TradeRepo
sig = TradeRepo().get_latest_signals()
if sig:
    print(f"  date={sig.get('date')} targets={len(sig.get('targets',[]))}")
    for t in sig.get('targets', []):
        print(f"    {t}")
else:
    print("  None")
