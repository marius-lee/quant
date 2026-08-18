#!/usr/bin/env bash
# 独立诊断: train_lgb_model 中 all_dates 为何为空
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
import pandas as pd, numpy as np, os
from quant.utils.logger import get_logger
_log = get_logger("diag")

# 1. 完全复制 train_lgb_model 的加载逻辑
from quant.factor.store import FactorStore
from quant.factor.compute import get_factor_names

fn = get_factor_names(status_filter="backtesting")
fstore = FactorStore(db_path="quant/data/market.db")
cache_dir = fstore._cache_dir
avail = sorted(f.replace('.csv.gz', '') for f in os.listdir(cache_dir) if f.endswith('.csv.gz'))

import sqlite3
from quant.config.paths import MARKET_DB
_c = sqlite3.connect(MARKET_DB)
end = _c.execute('SELECT MAX(date) FROM daily').fetchone()[0]
_c.close()
start = '2025-07-01'
train_dates = [d for d in avail if start <= d <= end]
_log.info("Date range: %s → %s, %d train dates", start, end, len(train_dates))

factor_panels = {name: {} for name in fn}
for i, d in enumerate(train_dates):
    data = fstore.load(d, factor_names=fn)
    for name in fn:
        if name in data and not data[name].empty:
            factor_panels[name][d] = data[name]
    if (i+1) % 50 == 0:
        _log.info("  loaded %d/%d", i+1, len(train_dates))

_log.info("Loaded: %d factors have data", sum(1 for v in factor_panels.values() if v))

# Convert to DataFrames
factor_dfs = {name: pd.DataFrame(sd).T for name, sd in factor_panels.items() if sd}
_log.info("Converted: %d DataFrames", len(factor_dfs))

# Check each DF index
first_idx = None
for name, df in factor_dfs.items():
    idx = set(df.index)
    if first_idx is None:
        first_idx = idx
    else:
        first_idx = first_idx & idx
    if len(set(df.index)) < 10:
        _log.info("  %s: ONLY %d dates! Index: %s", name, len(df.index), list(df.index)[:5])

_log.info("Intersection size: %d dates", len(first_idx) if first_idx else 0)
_log.info("First DF indexes:")
for name, df in list(factor_dfs.items())[:3]:
    _log.info("  %s: %d dates range %s→%s", name, len(df.index), str(df.index[0]), str(df.index[-1]))
for name, df in list(factor_dfs.items())[-3:]:
    _log.info("  %s: %d dates range %s→%s", name, len(df.index), str(df.index[0]), str(df.index[-1]))
PYEOF
