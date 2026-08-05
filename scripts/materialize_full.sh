#!/usr/bin/env bash
# 全量重建因子缓存: 2019-07-01 → 2026-08-03
# fit skip 自动跳过已存在的日期
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 -c "
from quant.factor.store import FactorStore
from quant.data.store import DataStore
from quant.data.repos import UniverseRepo
from quant.config.paths import FACTOR_CACHE_DB
from quant.factor.compute import get_factor_names
import pandas as pd

store = DataStore()
fs = FactorStore(db_path=FACTOR_CACHE_DB)
dates = pd.date_range('2019-07-01', '2026-12-31', freq='B')
date_strs = [d.strftime('%Y-%m-%d') for d in dates]
factor_names = get_factor_names(status_filter='backtesting')
symbols = UniverseRepo().get_symbols(exclude_market='BJ')

print(f'{len(date_strs)} dates x {len(factor_names)} factors x {len(symbols)} symbols')
fs.materialize(date_strs, factor_names, symbols, store=store, force=True)
print('DONE')
"
