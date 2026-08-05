#!/usr/bin/env bash
# 补充缺失的因子缓存日期: 2026-08-04 → 2026-08-05
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 -c "
from quant.factor.store import FactorStore; from quant.data.store import DataStore
from quant.data.repos import UniverseRepo; from quant.factor.compute import get_factor_names
import pandas as pd
s=DataStore(); f=FactorStore()
dates=pd.date_range('2026-08-04','2026-08-05',freq='B')
f.materialize([d.strftime('%Y-%m-%d') for d in dates],
    get_factor_names(status_filter='backtesting'),
    UniverseRepo().get_symbols(exclude_market='BJ'),store=s,force=True)
print('DONE')
"
