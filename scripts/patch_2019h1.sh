#!/usr/bin/env bash
# 补建 2019 上半年因子缓存 (2019-01-01 → 2019-07-01)
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
from quant.factor.store import FactorStore
from quant.data.store import DataStore
from quant.data.repos import UniverseRepo
from quant.factor.compute import get_factor_names
import pandas as pd
s = DataStore()
f = FactorStore()
dates = pd.date_range('2019-01-01', '2019-07-01', freq='B')
f.materialize([d.strftime('%Y-%m-%d') for d in dates],
    get_factor_names(status_filter='backtesting'),
    UniverseRepo().get_symbols(exclude_market='BJ'), store=s, force=True)
print('DONE')
PYEOF
