#!/usr/bin/env python3
"""诊断：信号已生成但为何未买入。"""
import sqlite3, json

mdb = sqlite3.connect("quant/data/market.db")
mdb.row_factory = sqlite3.Row
tdb = sqlite3.connect("quant/data/trades.db")
tdb.row_factory = sqlite3.Row

# ── 1. 今日任务运行记录 ──
print("=" * 60)
print("1. 今日任务运行记录 (market.db.task_runs)")
print("=" * 60)
for r in mdb.execute(
    "SELECT task_name, status, started_at, finished_at, error, summary "
    "FROM task_runs WHERE date='2026-07-22' ORDER BY started_at DESC LIMIT 10"
).fetchall():
    err = (r["error"] or "")[:100]
    sm = (r["summary"] or "")[:100]
    print(f"  {r['task_name']:12s} {r['status']:10s} {r['started_at']} -> {r['finished_at']}")
    if err: print(f"    error: {err}")
    if sm: print(f"    summary: {sm}")

# ── 2. 今日信号 ──
print("\n" + "=" * 60)
print("2. 今日信号 (trades.db.daily_signals)")
print("=" * 60)
for r in tdb.execute(
    "SELECT date, mode, generated_at, signals_json, exec_notes "
    "FROM daily_signals WHERE date='2026-07-22' ORDER BY generated_at DESC LIMIT 3"
).fetchall():
    sigs = json.loads(r["signals_json"]) if r["signals_json"] else []
    print(f"  date={r['date']} mode={r['mode']} generated={r['generated_at']} sigs={len(sigs)}")
    print(f"  exec_notes={r['exec_notes']}")
    for s in sigs:
        print(f"    {s['symbol']} {s['side']} shares={s['shares']} price≈{s['price']}")

# ── 3. 挂单 ──
print("\n" + "=" * 60)
print("3. 挂单 (trades.db.pending_orders)")
print("=" * 60)
rows = tdb.execute(
    "SELECT symbol, side, target_shares, limit_price, status, placed_at, "
    "filled_shares, filled_price, filled_at, cancel_reason, chase_count, day "
    "FROM pending_orders WHERE day='2026-07-22' ORDER BY placed_at DESC"
).fetchall()
print(f"  rows: {len(rows)}")
for r in rows:
    d = dict(r)
    print(f"  {d['symbol']} {d['side']} target={d['target_shares']} limit={d['limit_price']} "
          f"status={d['status']} filled={d['filled_shares']}@{d['filled_price']} "
          f"cancel={d['cancel_reason']} chases={d['chase_count']}")

# ── 4. 已成交交易 ──
print("\n" + "=" * 60)
print("4. 已成交交易 (trades.db.sim_trades)")
print("=" * 60)
rows = tdb.execute(
    "SELECT date, symbol, side, price, shares, pnl, pnl_pct, mode, created_at "
    "FROM sim_trades WHERE date='2026-07-22' ORDER BY created_at DESC"
).fetchall()
print(f"  rows: {len(rows)}")
for r in rows:
    d = dict(r)
    print(f"  {d['date']} {d['symbol']} {d['side']} {d['shares']}@{d['price']} pnl={d['pnl']}")

# ── 5. 策略配置 ──
print("\n" + "=" * 60)
print("5. 策略配置 (trades.db.strategy_config)")
print("=" * 60)
rows = tdb.execute("SELECT * FROM strategy_config WHERE mode='live'").fetchall()
for r in rows:
    print(f"  {dict(r)}")

# ── 6. 资金 ──
print("\n" + "=" * 60)
print("6. 最近资金快照 (trades.db.daily_equity)")
print("=" * 60)
for r in tdb.execute("SELECT * FROM daily_equity ORDER BY date DESC LIMIT 3").fetchall():
    print(f"  {dict(r)}")

mdb.close()
tdb.close()
