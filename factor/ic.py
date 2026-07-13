"""统一 IC 计算模块 — 唯一入口。

消费者:
  - backtest/loop.py: 回测 walk-forward IC 重算 → result["ic_map"]
  - backtest/diagnostics.py: 因子归因 → result["ic_means"], result["ic_irs"]
  - factor/stats_cache.py: Phase 2 因子评估 → result["ic_means"], result["ic_irs"],
    result["ic_series"], result["ic_decay"]

设计: compute_ic() 是唯一公开函数，支持两种模式:
  Mode A — 取数据+算因子 (backtest): 传入 factor_names, date, symbols, store, lookback
  Mode B — 预计算因子值 (Phase 2): 传入 factor_values, forward_1d, forward_5d, forward_20d

来源: Grinold & Kahn (1999) Ch6, ADR 029 (四层回测)
"""

import pandas as pd
import numpy as np
from utils.logger import get_logger
from config.loader import get as cfg

_log = get_logger("factor.ic")


# ── 私有: 共享 Spearman IC 计算 ──

def _spearman_ic(fv_series, fwd_series, min_obs=30):
    """计算因子值与 forward return 的横截面 Spearman 秩相关系数。
    双方共用: compute_ic (Mode A) 和 compute_ic (Mode B) 都调用此函数。
    """
    if np.std(fv_series) < 1e-10 or np.std(fwd_series) < 1e-10:
        return None
    common = fv_series.dropna().index.intersection(fwd_series.dropna().index)
    if len(common) < min_obs:
        return None
    from scipy import stats as _stats
    rho, _ = _stats.spearmanr(fv_series.loc[common], fwd_series.loc[common])
    return float(rho) if not np.isnan(rho) else None


# ── 公开: 统一 IC 计算 ──

def compute_ic(*,
               # Mode A: 取数据+算因子
               factor_names=None,
               date=None,
               symbols=None,
               store=None,
               lookback=60,
               status_filter="backtesting",
               # Mode B: 预计算因子值
               factor_values=None,
               forward_1d=None,
               forward_5d=None,
               forward_20d=None,
               # 公共参数
               min_periods=30,
               ):
    """统一 IC 计算 — 单一入口。

    两种模式互斥:
      Mode A: 传入 factor_names + date + symbols → 取数据、算因子、算 IC
      Mode B: 传入 factor_values + forward_1d → 直接用预计算值算 IC

    Returns dict with keys:
      ic_means, ic_irs, ic_series, ic_decay, n_valid, n_positive, ic_map
    """
    # ── Mode B: 预计算因子值 ──
    if factor_values is not None:
        return _compute_ic_from_values(
            factor_values, forward_1d, forward_5d, forward_20d, min_periods
        )

    # ── Mode A: 取数据+算因子 ──
    if factor_names is None or date is None or symbols is None:
        raise ValueError("compute_ic: factor_names+date+symbols required for Mode A")

    if store is None:
        from data.store import DataStore
        store = DataStore()

    n = len(factor_names)
    if n == 0:
        return {"ic_means": {}, "ic_irs": {}, "ic_series": {}, "ic_decay": {},
                "n_valid": 0, "n_positive": 0, "ic_map": {}}

    _log.info("compute_ic: %d factors x %d stocks, lookback=%dd before %s",
              n, len(symbols), lookback, date)

    from factor.compute import compute_all_factors

    end_dt = pd.Timestamp(date)
    from factor.windows import max_factor_calendar_days
    _ic_factor_min = max_factor_calendar_days(factor_names)
    start_dt = end_dt - pd.Timedelta(days=max(lookback * 2, _ic_factor_min))
    all_dates = pd.bdate_range(start=start_dt, end=end_dt)
    trading_days = []
    for d in reversed(all_dates):
        ds = d.strftime("%Y-%m-%d")
        trading_days.append(ds)
        if len(trading_days) >= lookback + 5:
            break
    if len(trading_days) < 30:
        _log.warning("compute_ic: only %d trading days before %s, skipping", len(trading_days), date)
        return {"ic_means": {}, "ic_irs": {}, "ic_series": {}, "ic_decay": {},
                "n_valid": 0, "n_positive": 0, "ic_map": {}}

    _log.info("compute_ic: %d trading days available before %s", len(trading_days), date)

    # ── ztd 预计算缓存: 消除 IC 计算每交易日重复 SQL 查询 ──
    from factor.compute.price._alternative import preload_ztd_cache as _preload_ztd_ic
    _preload_ztd_ic(trading_days, symbols)

    factor_daily = {name: {} for name in factor_names}
    fwd_1d = {}

    for ds in trading_days[lookback // 2:]:
        try:
            data = store.get_daily(symbols, ds)
            if data is None or data.empty:
                continue
            close = data["close"]
            if not isinstance(close, pd.DataFrame):
                continue
            if len(close) < 2:
                continue
            fwd = (close.iloc[-1] / close.iloc[-2]) - 1
            fundamentals = store.get_fundamentals(symbols, ds)
            factor_vals = compute_all_factors(
                data, ds, fundamentals=fundamentals,
                factor_names=factor_names, status_filter=status_filter,
            )
            for name, series in factor_vals.items():
                if isinstance(series, pd.Series):
                    factor_daily[name][ds] = series
            fwd_1d[ds] = fwd
        except Exception as e:
            _log.debug("compute_ic: %s failed: %s", ds, e)

    # 转为 Mode B 格式 → 统一 IC 计算
    fv_dict = {}
    for name in factor_names:
        d = factor_daily.get(name, {})
        if len(d) >= min_periods:
            fv_dict[name] = d

    fwd_1d_df = pd.DataFrame(fwd_1d).T if fwd_1d else pd.DataFrame()

    result = _compute_ic_from_values(fv_dict, fwd_1d_df, None, None, min_periods)

    # 构建 ic_map (back compat: 归一化权重)
    ic_map = {}
    for name in factor_names:
        ic_mean = result["ic_means"].get(name, 0.0)
        ic_ir = result["ic_irs"].get(name, 0.0)
        n_obs = len(result["ic_series"].get(name, {}))
        weight = abs(ic_ir) if ic_mean > 0 else 0.0
        ic_map[name] = {
            "ic_mean": round(ic_mean, 4),
            "ic_ir": round(ic_ir, 3),
            "weight": round(weight, 4),
            "n_obs": n_obs,
        }
    total_w = sum(v["weight"] for v in ic_map.values())
    if total_w > 0:
        for v in ic_map.values():
            v["weight"] = round(v["weight"] / total_w, 4)
    result["ic_map"] = ic_map

    if store:
        try:
            store.close()
        except Exception:
            import logging; logging.getLogger("quant.factor.ic").warning("store.close() failed in merge_ic_to_registry", exc_info=True)

    return result


# ── 私有: 从预计算因子值算 IC ──

def _compute_ic_from_values(factor_values_by_date, forward_1d, forward_5d=None,
                            forward_20d=None, min_periods=30):
    """从预计算因子值计算 IC (统一内部实现)。

    Returns dict: {ic_means, ic_irs, ic_series, ic_decay, n_valid, n_positive}
    """
    ic_means = {}
    ic_irs = {}
    ic_series = {}
    ic_decay = {}

    for name, fv_dict in factor_values_by_date.items():
        if len(fv_dict) < min_periods:
            continue

        # 1d IC
        ics = []
        ic_by_date = {}
        for date_str, fv_series in fv_dict.items():
            if isinstance(forward_1d, pd.DataFrame) and date_str in forward_1d.index:
                fr = forward_1d.loc[date_str].dropna()
                if isinstance(fr, pd.DataFrame):
                    fr = fr.iloc[0]
                rho = _spearman_ic(fv_series, fr, min_obs=30)
                if rho is not None:
                    ics.append(rho)
                    ic_by_date[date_str] = rho

        if ics:
            ic_arr = np.array(ics)
            ic_means[name] = float(np.mean(ic_arr))
            ic_irs[name] = float(np.mean(ic_arr) / np.std(ic_arr, ddof=1)) if np.std(ic_arr, ddof=1) > 0 else 0.0
        else:
            ic_means[name] = 0.0
            ic_irs[name] = 0.0
        ic_series[name] = ic_by_date

        # Multi-horizon decay
        decay = {}
        for horizon, fwd_df in [("1d", forward_1d),
                                ("5d", forward_5d),
                                ("20d", forward_20d)]:
            if fwd_df is None or (hasattr(fwd_df, 'empty') and fwd_df.empty):
                decay[horizon] = 0.0
                continue
            h_ics = []
            for date_str, fv_series in fv_dict.items():
                if not isinstance(fwd_df, pd.DataFrame) or date_str not in fwd_df.index:
                    continue
                fr = fwd_df.loc[date_str].dropna()
                if isinstance(fr, pd.DataFrame):
                    fr = fr.iloc[0]
                rho = _spearman_ic(fv_series, fr, min_obs=20)
                if rho is not None:
                    h_ics.append(rho)
            decay[horizon] = float(np.mean(h_ics)) if h_ics else 0.0
        ic_decay[name] = decay

    n_valid = len(ic_means)
    n_positive = sum(1 for v in ic_means.values() if v > 0)
    _log.info("compute_ic done: %d/%d valid, %d positive IC",
              n_valid, len(factor_values_by_date), n_positive)

    return {
        "ic_means": ic_means,
        "ic_irs": ic_irs,
        "ic_series": ic_series,
        "ic_decay": ic_decay,
        "n_valid": n_valid,
        "n_positive": n_positive,
    }
