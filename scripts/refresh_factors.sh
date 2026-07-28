#!/usr/bin/env bash
# 刷新因子评估缓存
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONPATH=. .venv/bin/python3 -c "
from quant.factor.stats_cache import get_cached_factor_stats
stats = get_cached_factor_stats(force_refresh=True)
print(f'Done: {len(stats.get(\"factor_keys\",[]))} factors, IC non-zero: {sum(1 for v in stats.get(\"ic\",[]) if v!=0)}')
"
