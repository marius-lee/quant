#!/usr/bin/env python3
"""因子冒烟测试 (test-v353)."""
import sys, pandas as pd, numpy as np, sqlite3
from quant.data.store import DataStore
from quant.data.repos.universe_repo import UniverseRepo
from quant.factor.compute.price import _PRICE_FN_MAP
from quant.factor.compute.fundamental import _FUNDAMENTAL_FN_MAP

store = DataStore()
symbols = UniverseRepo().get_symbols(exclude_market='BJ')[:200]
df = store.get_daily(symbols, start='2020-01-01', end='2020-01-10')
d = '2020-01-07'
sl = df.loc[:pd.Timestamp(d)]

# fundamentals: stocks 表 (含 pe/pb/total_mv/roe/high_52w)
# close_latest 从 daily OHLCV 取
conn = sqlite3.connect('quant/data/market.db')
ph = ','.join('?' * len(symbols))
rows = conn.execute(f"""
    SELECT symbol, pe, pb, total_mv, roe, high_52w
    FROM stocks WHERE symbol IN ({ph})
""", symbols).fetchall()
conn.close()

vals = {}
for r in rows:
    vals[r[0]] = {'pe': r[1], 'pb': r[2], 'total_mv': r[3], 'roe': r[4], 'high_52w': r[5]}
fundamentals = pd.DataFrame(vals).T
fundamentals.index.name = 'symbol'

# close_latest: 从 daily 取最新 close
close_df = df['close']
latest_close = close_df.iloc[-1]
fundamentals['close_latest'] = latest_close
store.close()

errors = []
for name, (fn, win) in _PRICE_FN_MAP.items():
    try:
        fn(sl, d, win) if win is not None else fn(sl, d)
    except Exception:
        errors.append(f'price/{name}')

for name, (cat, fn) in _FUNDAMENTAL_FN_MAP.items():
    try:
        fn(fundamentals, d)
    except Exception:
        errors.append(f'fundamental/{name}')

total = len(_PRICE_FN_MAP) + len(_FUNDAMENTAL_FN_MAP)
passed = total - len(errors)
print(f'{passed}/{total} passed')
for e in sorted(errors):
    print(f'  FAIL {e}')
sys.exit(0 if not errors else 1)
