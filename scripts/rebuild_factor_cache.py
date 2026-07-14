"""P0-2: 重建 factor_cache.json — 前端因子分析 + IC 加权所需。

数据修复完成后运行此脚本。
n_symbols=800: ~3-5 分钟 (中证800, 默认)
n_symbols=1000: ~5-8 分钟 (更广覆盖)
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from quant.factor.stats_cache import force_refresh_cache

print("Rebuilding factor_cache.json with 800 stocks...")
stats = force_refresh_cache(n_symbols=800)

print(f"\nDone: {len(stats.get('factors', []))} factors evaluated")
print(f"Cached at: {stats.get('cached_at', '?')[:19]}")
print("\nFactor IC rankings:")
for i, name in enumerate(stats.get('factors', [])):
    ic = stats['ic'][i]
    ir = stats['ic_ir'][i]
    key = stats.get('factor_keys', [name])[i]
    cat = stats.get('meta', {}).get(key, {}).get('category', '?')
    bar = '█' * max(1, int(abs(ic) * 200))
    print(f"  {name:20s} {cat:8s} IC={ic:+.4f} IR={ir:+.2f} {bar}")
