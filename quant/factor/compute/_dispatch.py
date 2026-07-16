"""Factor compute dispatcher — compute_all_factors."""

import pandas as pd
from typing import Optional

from quant.utils.logger import get_logger
from quant.factor.compute._preload import preload_aux_data
from quant.factor.registry import _FIN_FACTORS
from quant.factor.compute.price import _PRICE_FN_MAP
from quant.factor.compute.fundamental import _FUNDAMENTAL_FN_MAP
from quant.factor.compute._registry import load_active_price_factors, load_active_fundamental_factors
import inspect

def compute_all_factors(data: pd.DataFrame, date: str,
                      primitives: dict = None,
                      fundamentals: pd.DataFrame = None,
                      benchmark_ret: Optional["pd.Series"] = None,
                      factor_names: list = None,
                      status_filter: str = "using",
                      preloaded_financials: pd.DataFrame = None,
                      preloaded_fundamentals: pd.DataFrame = None) -> dict:
    """批量计算所有已注册因子 -> {factor_name: Series(index=symbol)}。

    价格因子从 data 计算, 基本面因子从 fundamentals 计算。
    benchmark_ret 用于特质波动率因子(对指数回归取残差)。
    """
    results = {}
    if factor_names is not None:
        price_factors = {n: ('dynamic', _PRICE_FN_MAP[n][1], _PRICE_FN_MAP[n][0])
                        for n in factor_names if n in _PRICE_FN_MAP}
        fund_factors = {n: _FUNDAMENTAL_FN_MAP[n]
                       for n in factor_names if n in _FUNDAMENTAL_FN_MAP}
    else:
        price_factors = load_active_price_factors(status_filter=status_filter)
        fund_factors = load_active_fundamental_factors(status_filter=status_filter)

    total_pf = len(price_factors)
    done_pf = 0
    _plog = get_logger("factor.compute")
    import time as _time
    _t0 = _time.time()

    # Preload auxiliary data (margin, analyst, financials) once for all factors
    _syms = list(data.columns.get_level_values(1).unique())
    if not _syms:
        _plog.warning('  no symbols in data, skipping factor computation')
        return {}
    _aux = preload_aux_data(_syms, date)
    if _aux:
        _plog.info("  aux data preloaded: %d tables", len(_aux))

    for name, (cat, win, fn) in price_factors.items():
        _plog.info(f"  computing {name}...")
        # 优先使用预计算算子
        from quant.factor.compute._primitives import FACTOR_SHORTCUT
        fn_name = getattr(fn, '__name__', '')
        if primitives is not None and fn_name in FACTOR_SHORTCUT:
            shortcut_result = FACTOR_SHORTCUT[fn_name](primitives, date, win)
            if shortcut_result is not None:
                results[name] = shortcut_result
                done_pf += 1
                if done_pf % 5 == 0 or done_pf == total_pf:
                    _plog.info(f"  price factors: {done_pf}/{total_pf} ({done_pf*100//total_pf}%, {_time.time()-_t0:.0f}s)")
                continue
        # fallback: 原始因子函数
        kwargs = {}
        if 'idio_vol' in name and benchmark_ret is not None:
            kwargs['benchmark_ret'] = benchmark_ret
        _sig = inspect.signature(fn)
        if 'aux' in _sig.parameters:
            kwargs['aux'] = _aux
        results[name] = fn(data, date, win, **kwargs)
        done_pf += 1
        if done_pf % 5 == 0 or done_pf == total_pf:
            _plog.info(f"  price factors: {done_pf}/{total_pf} ({done_pf*100//total_pf}%, {_time.time()-_t0:.0f}s)")
    _plog.info(f"  price factors done: {total_pf} in {_time.time()-_t0:.0f}s")

    if fundamentals is not None and not fundamentals.empty:
        financials = None
        if fundamentals is not None and any(n in fund_factors for n in _FIN_FACTORS):
            if preloaded_financials is not None:
                financials = preloaded_financials.get(date)
            else:
                from quant.data.store import DataStore
                store = DataStore()
                financials = store.get_financials(fundamentals.index.tolist(), date=date)
                store.close()
        total_ff = len(fund_factors)
        done_ff = 0
        import time as _time2
        _t1 = _time2.time()
        for name, (cat, fn) in fund_factors.items():
            _plog.info(f"  computing {name}...")
            kwargs = {}
            if name in _FIN_FACTORS and financials is not None:
                kwargs['financials'] = financials
            _sig = inspect.signature(fn)
            _fn_kwargs = {}
            if 'aux' in _sig.parameters:
                _fn_kwargs['aux'] = _aux
            if kwargs and 'financials' in _sig.parameters:
                _fn_kwargs['financials'] = kwargs['financials']
            results[name] = fn(fundamentals, date, **_fn_kwargs)
            done_ff += 1
            if done_ff % 5 == 0 or done_ff == total_ff:
                _plog.info(f"  fundamental factors: {done_ff}/{total_ff} ({done_ff*100//total_ff}%, {_time2.time()-_t1:.0f}s)")
        _plog.info(f"  fundamental factors done: {total_ff} in {_time2.time()-_t1:.0f}s")
    return results

# 7. 基本面因子 — Fama & French (1992, 1993, 2015)
# ═══════════════════════════════════════════════════════════
