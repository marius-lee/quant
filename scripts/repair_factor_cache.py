#!/usr/bin/env python3
"""修复损坏的因子缓存：删除残留 manifest，全量重建缺失/损坏的日期。

用法:
    cd /Users/mariusto/project/quant
    .venv/bin/python scripts/repair_factor_cache.py
"""
import time, os, glob

def main():
    from quant.factor.store import FactorStore
    from quant.data.store import DataStore
    from quant.data.repos.universe_repo import UniverseRepo
    from quant.factor.compute import get_factor_names

    t0 = time.monotonic()

    # 1. 清理所有残留 manifest（上次崩溃留下的）
    fs = FactorStore()
    removed = 0
    for mf in glob.glob(os.path.join(fs._cache_dir, '*.manifest.json')):
        os.remove(mf)
        removed += 1
    print(f"Removed {removed} stale manifests")

    # 2. 找出缺失或损坏的日期
    store = DataStore()
    all_dates = [r[0] for r in store._connect().execute(
        "SELECT DISTINCT date FROM daily WHERE date >= '2020-01-01' AND date <= '2026-07-31' ORDER BY date"
    ).fetchall()]

    missing = []
    damaged = []
    for d in all_dates:
        path = fs._path(d)
        if not os.path.exists(path):
            missing.append(d)
        else:
            factors = fs._get_existing_factors(d)
            if len(factors) < 20:  # 正常应该有 22-23 个因子
                damaged.append(d)
                os.remove(path)  # 删除损坏文件
                os.remove(fs._manifest_path(d)) if os.path.exists(fs._manifest_path(d)) else None

    broken = sorted(set(missing + damaged))
    print(f"Missing files: {len(missing)}, Damaged: {len(damaged)}, Total to repair: {len(broken)}")

    if not broken:
        print("No damage found. Cache is healthy.")
        store.close()
        return

    # 3. 全量重建损坏日期（用全部因子，不只是新因子）
    factors = sorted(set(get_factor_names(status_filter='backtesting'))
                     | set(get_factor_names(status_filter='using')))
    symbols = UniverseRepo().get_symbols(exclude_market='BJ')

    print(f"Re-materializing {len(broken)} dates x {len(factors)} factors x {len(symbols)} symbols")
    print(f"Date range: {broken[0]} → {broken[-1]}")

    r = fs.materialize(broken, factors, symbols, force=True, chunk_days=200)

    elapsed = time.monotonic() - t0
    print(f"Done: {r['n_dates']} dates, {r['n_rows']} rows, {elapsed:.0f}s")
    store.close()

if __name__ == '__main__':
    main()
