#!/usr/bin/env python3
"""烟雾测试: 调用所有因子函数, 抓出任何运行时错误。

用法:
  cd /Users/mariusto/project/quant
  PYTHONPATH=. .venv/bin/python3 scripts/smoke_test.py

退出码: 0=全部通过, 1=有错误.
每次修改 factor/compute.py 或 factor/stats_cache.py 后必须运行此测试.
"""
import sys, os, time, traceback, pandas as pd, numpy as np

def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    from data.store import DataStore
    store = DataStore()

    # 50 只成交活跃的股票
    rows = store._connect().execute("""
        SELECT symbol FROM daily WHERE date >= date('now','-120 days')
        GROUP BY symbol HAVING COUNT(*)>=60 ORDER BY AVG(amount) DESC LIMIT 50
    """).fetchall()
    symbols = [r[0] for r in rows]

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    start = (pd.Timestamp.today() - pd.Timedelta(days=300)).strftime("%Y-%m-%d")
    data = store.get_daily(symbols, start=start, end=end)
    dates = sorted(data.index.get_level_values(0).unique())
    test_date = dates[-1]
    test_date_str = test_date.strftime("%Y-%m-%d")

    fundamentals = store.get_fundamentals(symbols, date=test_date_str)
    fin = store.get_financials(symbols, date=test_date_str)
    store.close()

    errors = []

    # ── 测试 compute_all_factors ──
    from factor.compute import compute_all_factors, _PRICE_FN_MAP, _FUNDAMENTAL_FN_MAP
    t0 = time.monotonic()
    try:
        fv = compute_all_factors(data, test_date_str, fundamentals=fundamentals,
                                 preloaded_financials={test_date_str: fin} if fin is not None else None)
    except Exception as e:
        errors.append(f"compute_all_factors CRASH: {e}")

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
        print(f"FAIL — {len(errors)} error(s) in {elapsed:.1f}s:")
        for e in errors:
            print(f"  ❌ {e}")
        return 1
    else:
        print(f"PASS — {len(_PRICE_FN_MAP)} price + {len(_FUNDAMENTAL_FN_MAP)} fund factors OK ({elapsed:.1f}s)")
        return 0

if __name__ == "__main__":
    sys.exit(main())

# ── 测试 stats_cache worker (单进程顺序) ──
def test_stats_cache():
    """模拟 worker: 小数据直接调 _pp_compute_chunk, 绕过 ProcessPoolExecutor."""
    from factor.stats_cache import _pp_compute_chunk
    from data.store import DataStore
    store = DataStore()
    rows = store._connect().execute("""
        SELECT symbol FROM daily WHERE date >= date('now','-30 days')
        GROUP BY symbol HAVING COUNT(*)>=5 ORDER BY AVG(amount) DESC LIMIT 10
    """).fetchall()
    syms = [r[0] for r in rows]
    store.close()
    # Get 2 recent dates
    store2 = DataStore()
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    start = (pd.Timestamp.today() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    d = store2.get_daily(syms, start=start, end=end)
    dates = sorted(d.index.get_level_values(0).unique())[-2:]
    ds_list = [dt.strftime("%Y-%m-%d") if hasattr(dt,'strftime') else str(dt)[:10] for dt in dates]
    store2.close()
    
    from factor.compute import get_factor_names
    fn = get_factor_names(status_filter=None)
    
    results = _pp_compute_chunk((syms, ds_list, fn))
    errs = [(d, e) for d, _, e in results if e]
    return errs

stats_errs = test_stats_cache()
if stats_errs:
    for d, e in stats_errs:
        errors.append(f"STATS  worker FAIL at {d}: {e}")
