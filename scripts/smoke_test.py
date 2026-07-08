#!/usr/bin/env python3
"""烟雾测试: 调用所有因子函数 + ProcessPoolExecutor 真实多进程路径。

用法:
  cd /Users/mariusto/project/quant
  PYTHONPATH=. .venv/bin/python3 scripts/smoke_test.py

退出码: 0=全部通过, 1=有错误.
每次修改 factor/compute.py 或 factor/stats_cache.py 后必须运行此测试.
"""
import sys, os, time, traceback, pandas as pd, numpy as np


def _test_process_pool() -> list:
    """[SMOKE-2] ProcessPoolExecutor 真实多进程测试 — 10 股 × 3 天 × 2 chunks。

    macOS spawn 模式要求此函数 top-level 定义，子进程 pickle 可达。
    不绕过 ProcessPoolExecutor：真正 spawn 子进程，worker 各自加载 DB、计算因子。
    返回: 错误信息列表，空列表 = 通过。
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

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

    # 3 个最近交易日
    store2 = DataStore()
    data = store2.get_daily(syms,
                            start=(pd.Timestamp.today() - pd.Timedelta(days=60)).strftime("%Y-%m-%d"),
                            end=pd.Timestamp.today().strftime("%Y-%m-%d"))
    dates = sorted(data.index.unique())[-3:]
    date_strs = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
                 for d in dates]
    store2.close()

    # 分 2 个 chunks (确保 ≥2 个进程被启动)
    if len(date_strs) >= 2:
        chunks = [date_strs[:1], date_strs[1:]]
    else:
        chunks = [date_strs]
    chunks = [c for c in chunks if c]

    from factor.stats_cache import _pp_compute_chunk
    print(f"\n[SMOKE-2] {len(syms)} stocks × {len(date_strs)} dates × {len(factor_names)} factors")
    print(f"[SMOKE-2] {len(chunks)} chunks, launching ProcessPoolExecutor(max_workers={len(chunks)})...")

    errors = []
    with ProcessPoolExecutor(max_workers=len(chunks)) as executor:
        futures = {executor.submit(_pp_compute_chunk, (syms, chunk, factor_names)): chunk
                   for chunk in chunks}
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
                errors.append(f"ProcessPool CHUNK CRASH: {type(e).__name__}: {e}")
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
            print(f"  ❌ {e}")
        return 1
    else:
        print(f"[SMOKE-1] PASS — {len(_PRICE_FN_MAP)} price + {len(_FUNDAMENTAL_FN_MAP)} fund factors OK ({elapsed:.1f}s)")

    # ── [SMOKE-2] ProcessPoolExecutor 真实多进程测试 ──
    pp_errors = _test_process_pool()
    if pp_errors:
        print(f"\n[SMOKE-2] FAIL — {len(pp_errors)} error(s):")
        for e in pp_errors:
            print(f"  ❌ {e}")
        return 1
    else:
        print(f"[SMOKE-2] PASS — ProcessPoolExecutor OK")

    print(f"\n{'='*60}")
    print(f"ALL PASS ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
