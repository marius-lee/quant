#!/usr/bin/env bash
# 批量补充历史数据: daily_valuation + lhb_detail + benchmark + financials
# 每个源内部已有 skip-existing 逻辑, 不会重复拉取
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
import sys
print("[1/4] daily_valuation sync 2019-01-02 → 2025-11-30 (逐日日志略多, 正常)", flush=True)
from quant.data.em_valuation import sync_range as em_sync
em_sync(start="2019-01-02", end="2025-11-30")
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
