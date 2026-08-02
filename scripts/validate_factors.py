#!/usr/bin/env python3
"""因子冒烟测试 (test-v352)."""
import sys, pandas as pd, numpy as np
from quant.data.store import DataStore
from quant.data.repos.universe_repo import UniverseRepo
from quant.factor.compute.price import _PRICE_FN_MAP
from quant.factor.compute.fundamental import _FUNDAMENTAL_FN_MAP
from quant.factor.compute._preload import preload_aux_data

store = DataStore()
symbols = UniverseRepo().get_symbols(exclude_market='BJ')[:200]
df = store.get_daily(symbols, start='2020-01-01', end='2020-01-10')
d = '2020-01-07'
sl = df.loc[:pd.Timestamp(d)]

# 预加载 aux 数据（基本面因子需要）
aux = preload_aux_data(symbols, d)
fundamentals = aux.get('stocks')  # 含 pe/pb/close_latest/total_mv 等

store.close()

errors = []
for name, (fn, win) in _PRICE_FN_MAP.items():
    try:
        if win is not None:
            fn(sl, d, win)
        else:
            fn(sl, d)
    except Exception as e:
        errors.append(f'price/{name}: {type(e).__name__}: {e}')

for name, (cat, fn) in _FUNDAMENTAL_FN_MAP.items():
    try:
        fn(fundamentals, d)
    except Exception as e:
        errors.append(f'fundamental/{name}: {type(e).__name__}: {e}')

total = len(_PRICE_FN_MAP) + len(_FUNDAMENTAL_FN_MAP)
passed = total - len(errors)
print(f'\n{passed}/{total} passed')
if errors:
    for e in errors:
        print(f'  FAIL {e}')
    print(f'\n{len(errors)} failures')
    sys.exit(1)
else:
    print('ALL PASS')
