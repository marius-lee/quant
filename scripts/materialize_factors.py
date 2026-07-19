#!/usr/bin/env python3
"""因子物化 — 将全部 backtesting 因子值写入 factor_cache.db"""
from quant.utils.excepthook import setup; setup()
from quant.factor.compute import get_factor_names
from quant.data.repos.universe_repo import UniverseRepo
from quant.data.store import DataStore
from quant.factor.store import FactorStore

store = DataStore()
dates = [r[0] for r in store._connect().execute(
    'SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date',
    ('2026-01-01', '2026-07-17')).fetchall()]
symbols = UniverseRepo().get_symbols(exclude_market='BJ')
factors = get_factor_names(status_filter='backtesting')
store.close()

print(f'materializing {len(factors)} factors x {len(dates)} dates x {len(symbols)} symbols')
fs = FactorStore()
fs.materialize(dates, factors, symbols, force=False)
print('DONE')
