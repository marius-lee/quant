#!/usr/bin/env bash
# 批量补充历史数据: daily_valuation + lhb_detail + benchmark + financials
# 每个源内部已有 skip-existing 逻辑, 不会重复拉取
#
# ── 进度监控 ──
#   tail -f logs/quant.log | grep '"backfill"'
#   tail -f logs/quant.log | grep '"em_valuation"'
# ─────────────
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 -u << 'PYEOF'
import sys, time

# ═══ 必须在所有 quant 模块 import 之前初始化日志系统 ═══
from quant.utils.logger import get_logger
log = get_logger("backfill")

t_start = time.monotonic()

def _elapsed():
    """距脚本启动的已用时间 (人类可读)。"""
    s = int(time.monotonic() - t_start)
    m, s = divmod(s, 60)
    return f"{m}m{s:02d}s"

# ════════════════════════════════════════════════════════════════
# 1/4  daily_valuation (逐日拉取东方财富估值, 最耗时)
# ════════════════════════════════════════════════════════════════
log.info("[1/4] daily_valuation start — 2019-01-02 → 2025-11-30")
log.info("[1/4] 阶段1依赖 em_valuation 模块内部逐日进度, 可在 quant.log 中 grep em_valuation 查看")
print(f"[{_elapsed()}] [1/4] daily_valuation sync 2019-01-02 → 2025-11-30", flush=True)
t1 = time.monotonic()

from quant.data.em_valuation import sync_range as em_sync
em_sync(start="2019-01-02", end="2025-11-30")

t1_elapsed = time.monotonic() - t1
log.info(f"[1/4] daily_valuation done ({t1_elapsed:.0f}s)")
print(f"[{_elapsed()}] [1/4] daily_valuation DONE ({t1_elapsed:.0f}s)", flush=True)

# ════════════════════════════════════════════════════════════════
# 2/4  lhb_detail 龙虎榜 (按月, ~36s)
# ════════════════════════════════════════════════════════════════
log.info("[2/4] lhb_detail start — 2019-01 → 2024-12 (72 months)")
print(f"[{_elapsed()}] [2/4] lhb_detail sync...", flush=True)
t2 = time.monotonic()

from quant.data.lhb import sync_range as lhb_sync
lhb_sync(start_year=2019, start_month=1, end_year=2024, end_month=12)

t2_elapsed = time.monotonic() - t2
log.info(f"[2/4] lhb done ({t2_elapsed:.0f}s)")
print(f"[{_elapsed()}] [2/4] lhb DONE ({t2_elapsed:.0f}s)", flush=True)

# ════════════════════════════════════════════════════════════════
# 3/4  benchmark_daily (沪深300基准)
# ════════════════════════════════════════════════════════════════
log.info("[3/4] benchmark start — 000300")
print(f"[{_elapsed()}] [3/4] benchmark_daily...", flush=True)
t3 = time.monotonic()

from quant.data.benchmark import sync_benchmark
sync_benchmark("000300")

t3_elapsed = time.monotonic() - t3
log.info(f"[3/4] benchmark done ({t3_elapsed:.0f}s)")
print(f"[{_elapsed()}] [3/4] benchmark DONE ({t3_elapsed:.0f}s)", flush=True)

# ════════════════════════════════════════════════════════════════
# 4/4  financials 覆盖检查 (只读查询, 极快)
# ════════════════════════════════════════════════════════════════
log.info("[4/4] financials coverage check")
print(f"[{_elapsed()}] [4/4] financials coverage:", flush=True)
t4 = time.monotonic()

from quant.data.store import DataStore
s = DataStore()
c = s._connect()
for t in ['financial_income','financial_balance','financial_cash_flow']:
    r = c.execute(f"SELECT MIN(stat_date), MAX(stat_date), COUNT(*) FROM {t}").fetchone()
    msg = f"  {t}: {r[0]} → {r[1]} ({r[2]} rows)"
    log.info(msg)
    print(msg, flush=True)
c.close()

t4_elapsed = time.monotonic() - t4
log.info(f"[4/4] financials done ({t4_elapsed:.0f}s)")
print(f"[{_elapsed()}] [4/4] financials DONE ({t4_elapsed:.0f}s)", flush=True)

# ════════════════════════════════════════════════════════════════
total = time.monotonic() - t_start
log.info(f"=== ALL DONE — total {total:.0f}s ===")
print(f"[{_elapsed()}] === ALL DONE (total {total:.0f}s) ===", flush=True)
PYEOF
