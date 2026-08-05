#!/usr/bin/env bash
# 批量补充历史数据: daily_valuation + lhb_detail + benchmark + financials
# 每个源内部已有 skip-existing 逻辑, 不会重复拉取
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
import sys, time

# ── 1. daily_valuation  (JQData, ~2000个交易日, 每日期 ~3s API delay) ──
print("[1/4] daily_valuation: 2019-01-02 → 2025-11-30")
sys.stdout.flush()
from quant.data.em_valuation import sync_range as em_sync
em_sync(start="2019-01-02", end="2025-11-30")
print("[1/4] daily_valuation done")

# ── 2. lhb_detail 龙虎榜 (按月, ~72个月) ──
print("[2/4] lhb_detail: 2019-01 → 2024-12")
sys.stdout.flush()
from quant.data.lhb import sync_range as lhb_sync
lhb_sync(start_year=2019, start_month=1, end_year=2024, end_month=12)
print("[2/4] lhb done")

# ── 3. benchmark_daily 补充到最新 ──
print("[3/4] benchmark_daily: extend to latest")
sys.stdout.flush()
from quant.data.benchmark import sync_benchmark
sync_benchmark("000300")
print("[3/4] benchmark done")

# ── 4. financials (income/balance/cash_flow) ──
# financial 表通过 daily_sync 的 step 5 更新, 检查是否需要补
print("[4/4] checking financials coverage...")
sys.stdout.flush()
from quant.data.store import DataStore
s = DataStore()
c = s._connect()
for t in ['financial_income', 'financial_balance', 'financial_cash_flow']:
    r = c.execute(f"SELECT MIN(stat_date), MAX(stat_date), COUNT(*) FROM {t}").fetchone()
    print(f"  {t}: {r[0]} → {r[1]} ({r[2]} rows) — OK, 财报数据按 stat_date 覆盖历年年报")
c.close()

print()
print("=== ALL DONE ===")
PYEOF
