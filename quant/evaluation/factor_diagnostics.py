"""因子诊断扩展 — IC 衰减曲线 + 分位数回测 (P2-1, P2-2).

对标 Alphalens / WorldQuant 因子准入标准:
  P2-1: IC decay-by-lag 序列 → half-life 估计
  P2-2: 分位数分组收益 → 单调性检验

Usage:
    from quant.evaluation.factor_diagnostics import analyze_factor
    result = analyze_factor(factor_name, factor_values, forward_returns)
"""

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

_log = get_logger("evaluation.diagnostics")


def ic_decay_curve(
    factor_values: pd.DataFrame,
    forward_returns: pd.DataFrame,
    max_lag: int = 20,
) -> dict:
    """IC 衰减曲线: 逐日因子值 vs 滞后 1..max_lag 日前向收益的 Spearman IC。

    Args:
        factor_values: DataFrame(index=date, columns=symbol) — 因子值
        forward_returns: DataFrame(index=date, columns=symbol) — 前向收益率
        max_lag: 最大滞后期数

    Returns:
        {lags: [1..max_lag], ic_means: [...], half_life_days: int|None,
         decay_ratio: float|None} — half_life 是 IC 衰减到峰值一半的滞后天数
    """
    common_dates = factor_values.index.intersection(forward_returns.index)
    if len(common_dates) < 30:
        return {"lags": [], "ic_means": [], "half_life_days": None, "decay_ratio": None}

    ic_by_lag = {}
    for lag in range(1, max_lag + 1):
        ic_vals = []
        for i, d in enumerate(common_dates[:-lag]):
            fwd_d = common_dates[i + lag]
            fv = factor_values.loc[d].dropna()
            fr = forward_returns.loc[fwd_d].dropna()
            common = fv.index.intersection(fr.index)
            if len(common) < 30:
                continue
            ic, _ = spearmanr(fv[common], fr[common])
            if not np.isnan(ic):
                ic_vals.append(ic)
        ic_by_lag[lag] = np.mean(ic_vals) if ic_vals else np.nan

    lags = sorted(ic_by_lag.keys())
    ic_means = [ic_by_lag[k] for k in lags]

    # Half-life: 从 lag=1 IC 衰减到一半需要的期数
    half_life = None
    decay_ratio = None
    if ic_means and not np.isnan(ic_means[0]) and abs(ic_means[0]) > 0.001:
        peak = abs(ic_means[0])
        half_target = peak / 2
        for i, lag in enumerate(lags):
            if i > 0 and abs(ic_means[i]) < half_target:
                half_life = lag
                break
        decay_ratio = abs(ic_means[-1]) / peak if len(ic_means) > 1 and peak > 0 else None

    _log.info(f"IC decay: {len(ic_means)} lags, half_life={half_life}d, decay_ratio={decay_ratio}")
    return {
        "lags": lags,
        "ic_means": [round(float(v), 6) for v in ic_means],
        "half_life_days": half_life,
        "decay_ratio": round(float(decay_ratio), 4) if decay_ratio else None,
    }


def quantile_returns(
    factor_values: pd.DataFrame,
    forward_returns: pd.DataFrame,
    n_quantiles: int = 5,
) -> dict:
    """分位数回测: 按因子值分组, 计算各组前向收益均值 (Alphalens 标准诊断).

    Args:
        factor_values: DataFrame(index=date, columns=symbol)
        forward_returns: DataFrame(index=date, columns=symbol)
        n_quantiles: 分组数 (默认 5)

    Returns:
        {quantiles: {1..n_quantiles: mean_return}, spread: top−bottom,
         monotonic: bool} — spread > 0 且全序单调为健康因子
    """
    common_dates = factor_values.index.intersection(forward_returns.index)
    if len(common_dates) < 30:
        return {"quantiles": {}, "spread": None, "monotonic": False}

    all_quantile_rets = {q: [] for q in range(1, n_quantiles + 1)}

    for d in common_dates:
        fv = factor_values.loc[d].dropna()
        fr = forward_returns.loc[d].dropna()
        common = fv.index.intersection(fr.index)
        if len(common) < n_quantiles * 10:
            continue
        fv_c = fv[common]
        fr_c = fr[common]
        # 按因子值分位数分组
        labels = pd.qcut(fv_c, n_quantiles, labels=False, duplicates="drop") + 1
        for q in range(1, n_quantiles + 1):
            mask = labels == q
            if mask.sum() > 0:
                all_quantile_rets[q].append(fr_c[mask].mean())

    result = {}
    for q in range(1, n_quantiles + 1):
        if all_quantile_rets[q]:
            result[q] = round(float(np.mean(all_quantile_rets[q])), 6)
        else:
            result[q] = None

    # Spread + monotonic check
    valid = [v for v in result.values() if v is not None]
    spread = valid[-1] - valid[0] if len(valid) >= 2 else None
    monotonic = all(
        result.get(i) is not None and result.get(i + 1) is not None and result[i] <= result[i + 1]
        for i in range(1, n_quantiles)
    ) if len(valid) >= 3 else False

    _log.info(f"Quantile returns: n={n_quantiles}, spread={spread}, monotonic={monotonic}")
    return {
        "quantiles": result,
        "spread": round(float(spread), 6) if spread else None,
        "monotonic": monotonic,
    }


def analyze_factor(
    factor_name: str,
    factor_values: pd.DataFrame,
    forward_returns: pd.DataFrame,
    max_lag: int = None,
    n_quantiles: int = None,
) -> dict:
    """单因子综合诊断: IC 衰减 + 分位数收益。

    Returns:
        {name, n_dates, n_symbols_avg, ic_decay: {...}, quantile_returns: {...},
         health: "good"|"weak"|"poor"}
    """
    if max_lag is None:
        max_lag = _require_cfg("factor.evaluation.max_lag")
    if n_quantiles is None:
        n_quantiles = _require_cfg("factor.evaluation.n_quantiles")

    decay = ic_decay_curve(factor_values, forward_returns, max_lag)
    quantiles = quantile_returns(factor_values, forward_returns, n_quantiles)

    # 综合健康判定
    health = "good"
    if decay.get("half_life_days") and decay["half_life_days"] < 5:
        health = "weak"
    elif decay.get("half_life_days") and decay["half_life_days"] < 10:
        health = "weak"
    if not quantiles.get("monotonic"):
        if health == "good":
            health = "weak"
        else:
            health = "poor"

    common_d = factor_values.index.intersection(forward_returns.index)
    avg_symbols = int(factor_values.loc[common_d].notna().sum(axis=1).mean()) if len(common_d) > 0 else 0

    return {
        "name": factor_name,
        "n_dates": len(common_d),
        "n_symbols_avg": avg_symbols,
        "ic_decay": decay,
        "quantile_returns": quantiles,
        "health": health,
    }
