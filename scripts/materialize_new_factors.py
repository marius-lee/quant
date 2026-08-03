#!/usr/bin/env python3
"""补物化 seasonality_12m_1m + tail_risk 到现有缓存。

用法:
    cd /Users/mariusto/project/quant
    .venv/bin/python scripts/materialize_new_factors.py
"""
import time

def main():
    from quant.factor.store import FactorStore
    from quant.data.store import DataStore
    from quant.data.repos.universe_repo import UniverseRepo

    t0 = time.monotonic()
    store = DataStore()
    dates = [r[0] for r in store._connect().execute(
        "SELECT DISTINCT date FROM daily WHERE date >= '2020-01-01' AND date <= '2026-07-31' ORDER BY date"
    ).fetchall()]
    factors = ['seasonality_12m_1m', 'tail_risk']
    symbols = UniverseRepo().get_symbols(exclude_market='BJ')

    print(f"Materializing {len(factors)} factors: {factors}")
    print(f"{len(dates)} dates x {len(symbols)} symbols")

    # 清除残留的损坏 manifest
    import os, glob
    fs = FactorStore()
    for mf in glob.glob(os.path.join(fs._cache_dir, '*.manifest.json')):
        os.remove(mf)
    print("Cleared stale manifests")

    r = fs.materialize(dates, factors, symbols, force=True, chunk_days=200)
    elapsed = time.monotonic() - t0
    print(f"Done: {r['n_dates']} dates, {r['n_rows']} rows, {elapsed:.0f}s")
    store.close()

if __name__ == '__main__':
    main()
