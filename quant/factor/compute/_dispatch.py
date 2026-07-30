"""Factor compute dispatcher — compute_all_factors."""

import numpy as np
import pandas as pd
from typing import Optional
from quant.config.constants import _require_cfg

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
                      preloaded_fundamentals: pd.DataFrame = None,
                      factor_fail_fast: bool = True,
                      quiet: bool = False) -> dict:
    """批量计算所有已注册因子 -> {factor_name: Series(index=symbol)}。

    quiet=True: 抑制逐因子日志 (批量物化场景, 减少 I/O)。

    当 factor_fail_fast=False 时, 单个因子异常不阻塞其他因子 (用于批量诊断/IC 计算)。

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

    # 零 fallback: 如果要求了基本面因子但没传 fundamentals, 直接报错
    if fund_factors and fundamentals is None:
        raise ValueError(
            f"fundamental factors requested but no fundamentals data provided: "
            f"{list(fund_factors.keys())[:10]}..."
        )

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
    if _aux and not quiet:
        _plog.info("  aux data preloaded: %d tables", len(_aux))

    for name, (cat, win, fn) in price_factors.items():
        if not quiet:
            _plog.info(f"  computing {name}...")
        # 优先使用预计算算子 — 零 fallback: shortcut 必须成功
        from quant.factor.compute._primitives import FACTOR_SHORTCUT
        fn_name = getattr(fn, '__name__', '')
        if primitives is not None and fn_name in FACTOR_SHORTCUT:
            shortcut_result = FACTOR_SHORTCUT[fn_name](primitives, date, win)
            if shortcut_result is None:
                raise ValueError(
                    f"factor {name}: shortcut returned None — "
                    f"required primitive missing from precompute_primitives"
                )
            results[name] = shortcut_result
            done_pf += 1
            if not quiet and (done_pf % 5 == 0 or done_pf == total_pf):
                _plog.info(f"  price factors: {done_pf}/{total_pf} ({done_pf*100//total_pf}%, {_time.time()-_t0:.0f}s)")
            continue
        # 不在 shortcut 中 — 走原始因子函数
        kwargs = {}
        if 'idio_vol' in name and benchmark_ret is not None:
            kwargs['benchmark_ret'] = benchmark_ret
        _sig = inspect.signature(fn)
        if 'aux' in _sig.parameters:
            kwargs['aux'] = _aux
        if not factor_fail_fast:
            try:
                results[name] = fn(data, date, win, **kwargs)
            except Exception as _e:
                _plog.error(f"  factor {name} failed ({type(_e).__name__}: {_e}), skipping")
        else:
            results[name] = fn(data, date, win, **kwargs)
        done_pf += 1
        if done_pf % 5 == 0 or done_pf == total_pf:
            _plog.info(f"  price factors: {done_pf}/{total_pf} ({done_pf*100//total_pf}%, {_time.time()-_t0:.0f}s)")
    if not quiet:
        _plog.info(f"  price factors done: {total_pf} in {_time.time()-_t0:.0f}s")

    if fundamentals is not None and not fundamentals.empty:
        financials = None
        if fundamentals is not None and any(n in fund_factors for n in _FIN_FACTORS):
            if preloaded_financials is not None:
                financials = preloaded_financials.get(date)
                if financials is None:
                    if not quiet:
                        _plog.warning(f"No financials preloaded for {date}, fundamental factors will use DB fallback")
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
            if not quiet:
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
            if not factor_fail_fast:
                try:
                    results[name] = fn(fundamentals, date, **_fn_kwargs)
                except Exception as _e:
                    _plog.error(f"  fundamental factor {name} failed ({type(_e).__name__}: {_e}), skipping")
            else:
                results[name] = fn(fundamentals, date, **_fn_kwargs)
            done_ff += 1
            if not quiet and (done_ff % 5 == 0 or done_ff == total_ff):
                _plog.info(f"  fundamental factors: {done_ff}/{total_ff} ({done_ff*100//total_ff}%, {_time2.time()-_t1:.0f}s)")
        if not quiet:
            _plog.info(f"  fundamental factors done: {total_ff} in {_time2.time()-_t1:.0f}s")

        # ADR-035 audit: 季报真空期衰减
        # 基本面因子在季报发布间隔期 (最长4个月) 值不变，但其预测力随时间衰减。
        # 对每个基本面因子按距最近财报天数做指数衰减: value × exp(-λ × days)。
        # λ = factor.compute.earnings_decay_lambda (默认 ln(2)/90 ≈ 0.0077, 半衰期90天)。
        _decay_lambda = _require_cfg("factor.compute.earnings_decay_lambda")
        if _decay_lambda > 0 and fund_factors:
            _decayed = 0
            for name in list(results.keys()):
                if name not in fund_factors:
                    continue
                series = results[name]
                if not isinstance(series, pd.Series) or series.empty:
                    continue
                # 从 financials 获取每只股票的最新 stat_date
                _decayed_series = series.copy()
                for tbl_name in ["financial_income", "financial_balance", "financial_cashflow"]:
                    tbl = preloaded_financials.get(tbl_name) if preloaded_financials else None
                    if tbl is not None and not tbl.empty and "stat_date" in tbl.columns and "symbol" in tbl.columns:
                        latest = tbl.groupby("symbol")["stat_date"].max()
                        for sym in series.index:
                            if sym in latest.index:
                                try:
                                    days = max(0, (pd.Timestamp(date) - pd.Timestamp(latest[sym])).days)
                                    if days > 30:  # 30天内不衰减 (刚发布)
                                        _decayed_series[sym] *= np.exp(np.float64(-_decay_lambda * days))
                                except (ValueError, TypeError):
                                    pass
                        break  # 用一个表即可 (income 覆盖最广)
                if _decayed_series.notna().sum() > 0:
                    results[name] = _decayed_series
                    _decayed += 1
            if _decayed > 0:
                _plog.info(f"  earnings decay applied to {_decayed} factors "
                           f"(λ={_decay_lambda:.4f}, half-life={np.log(2)/_decay_lambda:.0f}d)")

    return results

# 7. 基本面因子 — Fama & French (1992, 1993, 2015)
# ═══════════════════════════════════════════════════════════
