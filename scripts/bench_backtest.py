#!/usr/bin/env python3
"""回测基准 — 采集 per-phase 耗时分布, 用于定位回测瓶颈。

用法:
  PYTHONPATH=. .venv/bin/python scripts/bench_backtest.py [start] [end]

输出: 回测总耗时 + 每调仓日 generate_signals 的 phases 聚合 (sync/load/factor/risk 等)。
幂等: 只读回测, 不写库 (strategy 用独立 DB)。
"""
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

START = sys.argv[1] if len(sys.argv) > 1 else "2025-01-01"
END = sys.argv[2] if len(sys.argv) > 2 else "2025-06-30"

# v500: 诊断脚本用 — dt_streak 因子个别交易日无数据 (评估中因子缺口),
# 会触发 loop 的 IC 缓存检查整批阻断。此处仅对基准搜索 monkeypatch 过滤,
# 不落库、不污染 factor_registry (market.db 被服务进程持有写锁)。
import quant.factor.compute as _fc
_orig_gen = _fc.get_factor_names
def _filtered_gen(status_filter=None):
    names = _orig_gen(status_filter)
    _exclude = {n for n in ("dt_streak",) if "--keep-dt-streak" not in sys.argv}
    return [n for n in names if n not in _exclude]
_fc.get_factor_names = _filtered_gen

# v500: UniverseRepo.get_symbols(start,end) 当前返回 0 (环境 data 缺口) →
# 基准 monkeypatch 忽略日期过滤, 仅测性能。不影响正式回测逻辑。
from quant.data.repos import UniverseRepo as _UR
_orig_syms = _UR.get_symbols
def _syms_no_date(self, exclude_market='BJ', **kw):
    return _orig_syms(self, exclude_market=exclude_market)
_UR.get_symbols = _syms_no_date

from quant.backtest.loop import run_backtest

t0 = time.time()
r = run_backtest(START, END, capital=5000)
elapsed = time.time() - t0

print(f"\n=== 回测基准 {START} → {END} ===")
print(f"总耗时: {elapsed:.1f}s")
print(f"交易日数: {len(r.get('equity_curve', []))}")
print("metrics:", r.get("metrics"))
print(f"signals_per_day: {len(r.get('signals_per_day', []))}")

# 聚合 phases (回测日志里逐日打印 phases=[...])
ph_agg = defaultdict(float)
ph_cnt = defaultdict(int)
for item in r.get("signals_per_day", []):
    ph = item.get("phases", [])
    for p in ph:
        name = p.get("name") if isinstance(p, dict) else getattr(p, "name", str(p))
        dur = (p.get("finished", 0) - p.get("started", 0)) if isinstance(p, dict) else 0
        ph_agg[name] += dur
        ph_cnt[name] += 1
if ph_agg:
    print("\n=== generate_signals per-phase 聚合 ===")
    for name, total in sorted(ph_agg.items(), key=lambda x: -x[1]):
        print(f"  {name:8s}: {total:8.1f}s  (x{ph_cnt[name]})")
else:
    print("\n(signals_per_day 无 phases 字段, 直接看日志 phases)")