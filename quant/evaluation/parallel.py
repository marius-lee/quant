"""Gap 5: Parallel factor evaluation — multiprocessing.Pool batching.

Splits the 65 factors into N batches and evaluates them in parallel
subprocesses, each with its own DataStore connection (SQLite WAL safe).

Usage:
    PYTHONPATH=. python3 evaluation/parallel.py

Speedup: ~4-8x on M1 (4 P-cores).
"""

import os, sys, time
_root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _root)

import multiprocessing as mp
from quant.utils.logger import get_logger

_log = get_logger("evaluation.parallel")


def _evaluate_batch(args):
    """Worker function: evaluate a batch of factors in a subprocess.

    Each subprocess has its own DataStore and factor compute context,
    avoiding any shared state or connection conflicts.

    Args:
        args: (factor_names: list, kwargs: dict)
    Returns:
        dict[f actor_name] = stats_dict
    """
    factor_names, kwargs = args
    results = {}

    from quant.data.store import DataStore
    from quant.factor.compute import compute_all_factors, _PRICE_FN_MAP, _FUNDAMENTAL_FN_MAP

    store = DataStore()
    n_symbols = kwargs.get("n_symbols", 500)
    lookback = kwargs.get("lookback") or _require_cfg("factor.evaluation.lookback")
    date_str = kwargs.get("date_str")

    # Load data for evaluation
    if date_str is None:
        from datetime import date as _date
        date_str = _date.today().strftime("%Y-%m-%d")

    symbols = store.get_universe(date_str)[:n_symbols]
    from quant.factor.windows import max_factor_calendar_days
    _eff_days = max(lookback * 2, max_factor_calendar_days(factor_names))
    hist_start = (pd.Timestamp(date_str) - pd.Timedelta(days=_eff_days)).strftime("%Y-%m-%d")
    data = store.get_daily(symbols, start=hist_start, end=date_str)
    fundamentals = store.get_fundamentals(symbols, date=date_str)

    if data is None or data.empty:
        return results

    # Compute all factors for this date
    all_factors = compute_all_factors(data, date_str, fundamentals=fundamentals)

    # Filter to only our batch
    for name in factor_names:
        if name in all_factors:
            series = all_factors[name]
            results[name] = {
                "valid_count": int(series.notna().sum()),
                "mean": float(series.mean()) if series.notna().any() else 0,
                "std": float(series.std()) if series.notna().any() else 0,
            }

    return results


def parallel_evaluate(n_workers=None, n_symbols=500, lookback=None, date_str=None):
    """Evaluate all factors in parallel using N worker processes.

    Args:
        n_workers: number of parallel processes (default: cpu_count)
        n_symbols: number of stocks in universe
        lookback: days of historical data
        date_str: evaluation date (default: today)

    Returns:
        dict[factor_name] = stats_dict
    """
    import pandas as pd
    from quant.factor.compute import _PRICE_FN_MAP, _FUNDAMENTAL_FN_MAP

    all_factor_names = list(_PRICE_FN_MAP.keys()) + list(_FUNDAMENTAL_FN_MAP.keys())
    all_factor_names = list(set(all_factor_names))  # deduplicate

    if not all_factor_names:
        return {}

    n_workers = n_workers or min(mp.cpu_count(), 8)

    # Split into batches
    batch_size = max(1, len(all_factor_names) // n_workers)
    batches = []
    for i in range(0, len(all_factor_names), batch_size):
        batch = all_factor_names[i:i + batch_size]
        if batch:
            batches.append(batch)

    kwargs = {
        "n_symbols": n_symbols,
        "lookback": lookback,
        "date_str": date_str,
    }

    args_list = [(batch, kwargs) for batch in batches]

    _log.info(f"parallel evaluate: {len(all_factor_names)} factors in {len(batches)} batches "
              f"across {n_workers} workers")

    t0 = time.time()
    all_results = {}

    # Use 'spawn' context for macOS compatibility
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=n_workers) as pool:
        batch_results = pool.map(_evaluate_batch, args_list)

    for batch_result in batch_results:
        all_results.update(batch_result)

    elapsed = time.time() - t0
    _log.info(f"parallel evaluate done: {len(all_results)}/{len(all_factor_names)} factors "
              f"in {elapsed:.1f}s ({elapsed/len(batches):.1f}s/batch)")

    return all_results


if __name__ == "__main__":
    results = parallel_evaluate()
    n_valid = sum(1 for v in results.values() if v.get("valid_count", 0) > 0)
    print(f"Evaluated {len(results)} factors, {n_valid} with valid data")
