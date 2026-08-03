#!/usr/bin/env python3
"""全量重建 stats_cache — 基于新物化完成的 factor_cache.

用法:
    cd /Users/mariusto/project/quant
    .venv/bin/python scripts/rebuild_stats_cache.py
"""
import time
from quant.factor.stats_cache import compute_factor_stats, force_refresh_cache

t0 = time.monotonic()

# Step 1: 重建 backtesting 池的 IC 统计
print("[1/2] Rebuilding IC stats for backtesting pool...")
stats_bt = compute_factor_stats(
    status_filter='backtesting',
    n_symbols=800,
    lookback=120,
)
n_bt = len(stats_bt.get('factors', []))
print(f"  backtesting: {n_bt} factors")

# Step 2: force_refresh_cache (写 factor_registry + 衰减检查)
print("[2/2] force_refresh_cache — syncing to factor_registry...")
stats_full = force_refresh_cache(n_symbols=800)
n_full = len(stats_full.get('factors', []))
print(f"  total: {n_full} factors")

# 抽样 IC
ic_map = stats_full.get('ic_map', {})
for name in list(ic_map.keys())[:8]:
    df = ic_map[name]
    if df is not None and len(df) > 0:
        try:
            ic_vals = df['ic'] if 'ic' in df.columns else df.iloc[:, 0]
            print(f"  {name}: {len(df)}d, IC_last={ic_vals.iloc[-1]:+.4f}, IC_20d={ic_vals.tail(20).mean():+.4f}")
        except Exception:
            print(f"  {name}: {len(df)} rows")

elapsed = time.monotonic() - t0
print(f"\nDone in {elapsed:.0f}s")

# 检查 active 因子
from quant.factor.compute import get_factor_names
for s in ['active', 'using', 'probation', 'evaluating', 'backtesting']:
    names = get_factor_names(status_filter=s)
    print(f"  {s}: {len(names)}")
