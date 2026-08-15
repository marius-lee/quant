#!/usr/bin/env bash
# 行业 PIT 生效后全量重物化因子缓存 (v502) — 2020-01-01 起.
#
# 用途: 同步 industry_history (行业 PIT) 完成后, 已有因子缓存仍含股票行业
#       后视快照 (industry_momentum / abn_turnover 中性化), 须 force 重物化.
# 版本: v502
# 用法: bash scripts/rematerialize_industry_pit.sh
# 幂等: force=True 全部覆盖重建, 可重复执行 (耗时约 1-2h, 后台跑建议 nohup).
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 - <<'EOF'
from quant.factor.store import FactorStore
from quant.data.store import DataStore
from quant.data.repos import UniverseRepo
from quant.config.paths import FACTOR_CACHE_DB
from quant.factor.compute import get_factor_names
import pandas as pd

store = DataStore()
fs = FactorStore(db_path=FACTOR_CACHE_DB)
# industry_history 起点 2020-01-01, 只重物化该区间 (2019 回测历史不依赖行业 PIT)
dates = pd.date_range('2020-01-01', pd.Timestamp.today().strftime('%Y-%m-%d'), freq='B')
date_strs = [d.strftime('%Y-%m-%d') for d in dates]
factor_names = get_factor_names(status_filter='backtesting')
symbols = UniverseRepo().get_symbols(exclude_market='BJ')

print(f'industry PIT rematerialize: {len(date_strs)} dates x '
      f'{len(factor_names)} factors x {len(symbols)} symbols')
r = fs.materialize(date_strs, factor_names, symbols, store=store, force=True)
print(f"rematerialize done: {r['n_rows']} rows in {r['elapsed_sec']:.1f}s")
EOF