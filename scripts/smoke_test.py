#!/usr/bin/env python3
"""烟雾测试: 调用所有因子函数 + ThreadPoolExecutor 多线程路径 (P78 纯线程).

用法:
  cd /Users/mariusto/project/quant
  PYTHONPATH=. .venv/bin/python3 scripts/smoke_test.py

退出码: 0=全部通过, 1=有错误.
每次修改 factor/compute.py 或 factor/stats_cache.py 后必须运行此测试.
"""
import sys, os, time, traceback, pandas as pd, numpy as np


def _test_thread_pool() -> list:
    """[SMOKE-2] ThreadPoolExecutor 多线程测试 — 10 股 × 3 天 × 2 chunks (P78).
    每个 worker 线程独立打开 DataStore, sqlite3 WAL 支持并发读。
    返回: 错误信息列表，空列表 = 通过。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from data.store import DataStore
    store = DataStore()
    rows = store._connect().execute("""
        SELECT symbol FROM daily WHERE date >= date('now','-30 days')
        GROUP BY symbol HAVING COUNT(*)>=5 ORDER BY AVG(amount) DESC LIMIT 10
    """).fetchall()
    syms = [r[0] for r in rows]
    store.close()

    from factor.compute import get_factor_names
    factor_names = get_factor_names(status_filter=None)

    store2 = DataStore()
    data = store2.get_daily(syms,
                            start=(pd.Timestamp.today() - pd.Timedelta(days=60)).strftime("%Y-%m-%d"),
                            end=pd.Timestamp.today().strftime("%Y-%m-%d"))
    dates = sorted(data.index.unique())[-3:]
    date_strs = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
                 for d in dates]
    store2.close()

    if len(date_strs) >= 2:
        chunks = [date_strs[:1], date_strs[1:]]
    else:
        chunks = [date_strs]
    chunks = [c for c in chunks if c]

    print(f"\n[SMOKE-2] {len(syms)} stocks x {len(date_strs)} dates x {len(factor_names)} factors")
    print(f"[SMOKE-2] {len(chunks)} chunks, launching ThreadPoolExecutor(max_workers={len(chunks)})...")

    # inline thread worker: each thread opens its own DataStore
    def _thread_worker(chunk_dates):
        import pandas as _pd
        from data.store import DataStore as _DS
        from factor.compute import compute_all_factors as _caf
        _store = _DS()
        data_start = (_pd.Timestamp(chunk_dates[0]) - _pd.Timedelta(days=365)).strftime("%Y-%m-%d")
        data_end = (_pd.Timestamp(chunk_dates[-1]) + _pd.Timedelta(days=40)).strftime("%Y-%m-%d")
        data = _store.get_daily(syms, start=data_start, end=data_end)
        results = []
        for d in chunk_dates:
            try:
                fundamentals = _store.get_fundamentals(syms, date=d)
                fin = _store.get_financials(syms, date=d)
                preloaded_fin = {d: fin} if fin is not None and not fin.empty else None
                fv = _caf(data, d, fundamentals=fundamentals,
                          factor_names=factor_names, preloaded_financials=preloaded_fin)
                results.append((d, fv, None))
            except Exception as e:
                results.append((d, {}, str(e)))
        _store.close()
        return results

    errors = []
    with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
        futures = {executor.submit(_thread_worker, chunk): chunk for chunk in chunks}
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                results = future.result()
                for date_str, fv_partial, err in results:
                    if err:
                        errors.append(f"[{date_str}] {err}")
                    else:
                        filled = sum(1 for s in fv_partial.values()
                                     if hasattr(s, 'dropna') and not s.dropna().empty)
                        print(f"  [SMOKE-2] {date_str}: {filled}/{len(factor_names)} factors OK")
            except Exception as e:
                errors.append(f"ThreadPool CHUNK CRASH: {type(e).__name__}: {e}")
                traceback.print_exc()

    return errors


def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from data.store import DataStore
    store = DataStore()

    # ── [SMOKE-1] 单进程单因子测试 ──
    rows = store._connect().execute("""
        SELECT symbol FROM daily WHERE date >= date('now','-120 days')
        GROUP BY symbol HAVING COUNT(*)>=60 ORDER BY AVG(amount) DESC LIMIT 50
    """).fetchall()
    symbols = [r[0] for r in rows]

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    start = (pd.Timestamp.today() - pd.Timedelta(days=300)).strftime("%Y-%m-%d")
    data = store.get_daily(symbols, start=start, end=end)
    dates = sorted(data.index.unique())
    test_date = dates[-1]
    test_date_str = test_date.strftime("%Y-%m-%d") if hasattr(test_date, "strftime") else str(test_date)[:10]

    fundamentals = store.get_fundamentals(symbols, date=test_date_str)
    fin = store.get_financials(symbols, date=test_date_str)
    store.close()

    errors = []

    # ── compute_all_factors ──
    from factor.compute import compute_all_factors, _PRICE_FN_MAP, _FUNDAMENTAL_FN_MAP
    t0 = time.monotonic()
    try:
        preloaded_fund = {test_date_str: fundamentals} if fundamentals is not None and not fundamentals.empty else None
        preloaded_fin = {test_date_str: fin} if fin is not None and not fin.empty else None
        fv = compute_all_factors(data, test_date_str, fundamentals=fundamentals,
                                 preloaded_fundamentals=preloaded_fund,
                                 preloaded_financials=preloaded_fin)
    except Exception as e:
        errors.append(f"compute_all_factors CRASH: {e}")
        traceback.print_exc()

    # ── 单测每个 price factor ──
    for name, (fn, win) in _PRICE_FN_MAP.items():
        try:
            result = fn(data, test_date_str, win)
        except Exception as e:
            errors.append(f"PRICE  {name}: {type(e).__name__}: {e}")

    # ── 单测每个 fundamental factor ──
    fin_factors = {"roe_reported", "roa", "debt_ratio", "accruals", "asset_growth", "gp_ta", "ocfp"}
    for name, (cat, fn) in _FUNDAMENTAL_FN_MAP.items():
        if fundamentals is None or fundamentals.empty:
            continue
        try:
            kwargs = {}
            if name in fin_factors and fin is not None:
                kwargs["financials"] = fin
            fn(fundamentals, test_date_str, **kwargs)
        except Exception as e:
            errors.append(f"FUND   {name}: {type(e).__name__}: {e}")

    elapsed = time.monotonic() - t0
    print(f"\n{'='*60}")
    if errors:
        print(f"[SMOKE-1] FAIL — {len(errors)} error(s) in {elapsed:.1f}s:")
        for e in errors:
            print(f"  X {e}")
        return 1
    else:
        print(f"[SMOKE-1] PASS — {len(_PRICE_FN_MAP)} price + {len(_FUNDAMENTAL_FN_MAP)} fund factors OK ({elapsed:.1f}s)")

    # ── [SMOKE-2] ThreadPoolExecutor 多线程测试 ──
    pp_errors = _test_thread_pool()
    if pp_errors:
        print(f"\n[SMOKE-2] FAIL — {len(pp_errors)} error(s):")
        for e in pp_errors:
            print(f"  X {e}")
        return 1
    else:
        print(f"[SMOKE-2] PASS — ThreadPoolExecutor OK")

    print(f"\n{'='*60}")
    print(f"ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
