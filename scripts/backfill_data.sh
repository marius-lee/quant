#!/usr/bin/env bash
# 批量补充历史数据: daily_valuation + lhb_detail + benchmark + financials
# 每个源内部已有 skip-existing 逻辑, 不会重复拉取
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
import sys, time

# ── 1. daily_valuation (JQData, ~2000交易日的daily表数据, 但只有未同步的才拉) ──
print("[1/4] daily_valuation sync...", flush=True)
from quant.data.em_valuation import sync_range as em_sync
from quant.data.store import DataStore
# 先统计待同步数量
s = DataStore()
c = s._connect()
all_dates = [r[0] for r in c.execute(
    "SELECT DISTINCT date FROM daily WHERE date >= '2019-01-02' AND date <= '2025-11-30' ORDER BY date").fetchall()]
synced = {r[0] for r in c.execute(
    "SELECT DISTINCT date FROM daily_valuation WHERE date >= '2019-01-02' AND date <= '2025-11-30'").fetchall()}
todo = [d for d in all_dates if d not in synced]
c.close()
print(f"  daily_valuation: {len(todo)} dates to sync ({len(all_dates)} total, {len(synced)} already done)", flush=True)
if todo:
    print(f"  first: {todo[0]}, last: {todo[-1]}, est ~{len(todo)*0.5:.0f}s", flush=True)
    em_sync(start="2019-01-02", end="2025-11-30")
else:
    print("  all already synced, skipping", flush=True)
print("[1/4] daily_valuation done", flush=True)

# ── 2. lhb_detail 龙虎榜 (按月) ──
print("[2/4] lhb_detail sync...", flush=True)
from quant.data.lhb import sync_range as lhb_sync
print("  lhb: 2019-01 → 2024-12 (72 months, ~36s)", flush=True)
lhb_sync(start_year=2019, start_month=1, end_year=2024, end_month=12)
print("[2/4] lhb done", flush=True)

# ── 3. benchmark_daily ──
print("[3/4] benchmark_daily...", flush=True)
from quant.data.benchmark import sync_benchmark
sync_benchmark("000300")
print("[3/4] benchmark done", flush=True)

# ── 4. financials 覆盖检查 ──
print("[4/4] financials coverage:", flush=True)
s = DataStore()
c = s._connect()
for t in ['financial_income','financial_balance','financial_cash_flow']:
    r = c.execute(f"SELECT MIN(stat_date), MAX(stat_date), COUNT(*) FROM {t}").fetchone()
    print(f"  {t}: {r[0]} → {r[1]} ({r[2]} rows)", flush=True)
c.close()

print("=== ALL DONE ===", flush=True)
PYEOF
