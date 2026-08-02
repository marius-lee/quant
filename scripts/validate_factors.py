#!/usr/bin/env python3
"""冒烟测试 — 所有因子计算函数 (test-v351)."""
import sys, pandas as pd
from quant.data.store import DataStore
from quant.data.repos.universe_repo import UniverseRepo
from quant.factor.compute.price import _PRICE_FN_MAP
from quant.factor.compute.fundamental import _FUNDAMENTAL_FN_MAP

store = DataStore()
symbols = UniverseRepo().get_symbols(exclude_market='BJ')[:50]
df = store.get_daily(symbols, start='2020-01-01', end='2020-01-10')
d = '2020-01-07'
sl = df.loc[:pd.Timestamp(d)]
store.close()

errors = 0
total = len(_PRICE_FN_MAP) + len(_FUNDAMENTAL_FN_MAP)

for name, (fn, win) in _PRICE_FN_MAP.items():
    try:
        if win is not None:
            fn(sl, d, win)
        else:
            fn(sl, d)
    except Exception as e:
        errors += 1
        print(f'FAIL price/{name}: {type(e).__name__}: {e}')

for name, (cat, fn) in _FUNDAMENTAL_FN_MAP.items():
    try:
        fn(sl, d)
    except Exception as e:
        errors += 1
        print(f'FAIL fundamental/{name}: {type(e).__name__}: {e}')

print(f'\n{total - errors}/{total} passed, {errors} failed')
sys.exit(0 if errors == 0 else 1)
