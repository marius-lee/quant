"""因子评估 — 截面 Rank IC、IC_IR、IC 衰减、相关性矩阵。

所有评估指标基于 Spearman 秩相关 (截面), 对异常值鲁棒。
来源: ② Grinold & Kahn (2000) Chapter 7 — Information Coefficient.
"""

import numpy as np
import pandas as pd
from scipy import stats
from typing import Optional
from factor.base import Factor, FactorStats


def rank_ic(factor_values: pd.Series, forward_returns: pd.Series) -> float:
    """计算单截面 Rank IC (Spearman 秩相关)。
    
    factor_values: index=symbol, 因子值
    forward_returns: index=symbol, 前瞻收益
    
    返回: Spearman ρ ∈ [-1, 1]
    来源: ② Grinold & Kahn (2000) — IC = corr(α, r_fwd)
    """
    common = factor_values.dropna().index.intersection(forward_returns.dropna().index)
    if len(common) < 30:
        return np.nan
    rho, _ = stats.spearmanr(
        factor_values.loc[common],
        forward_returns.loc[common]
    )
    return rho if not np.isnan(rho) else np.nan


def evaluate_factor(
    factor: Factor,
    factor_values: pd.Series,
    forward_returns: pd.Series,
    decay_horizons: Optional[list] = None,
) -> FactorStats:
    """评估单因子的完整统计量。
    
    factor_values: MultiIndex (date, symbol), 因子值
    forward_returns: MultiIndex (date, symbol), 前瞻收益
    decay_horizons: [1, 5, 20] — IC 衰减检查点
    
    返回: FactorStats 含 IC/IR/衰减。
    来源: ② Grinold & Kahn (2000); ② Qian, Hua, Sorensen (2007)
    """
    if decay_horizons is None:
        decay_horizons = [1, 5, 20]
    
    # 按日期分组计算 IC
    ic_series = []
    dates = factor_values.index.get_level_values(0).unique()
    
    for d in dates:
        fv = factor_values.loc[d]
        fr = forward_returns.loc[d] if d in forward_returns.index.get_level_values(0) else None
        if fr is None:
            continue
        ic = rank_ic(fv, fr)
        if not np.isnan(ic):
            ic_series.append(ic)
    
    ic_arr = np.array(ic_series)
    ic_mean = float(np.mean(ic_arr)) if len(ic_arr) > 0 else 0.0
    ic_std = float(np.std(ic_arr, ddof=1)) if len(ic_arr) > 1 else 0.0
    ic_ir = ic_mean / ic_std if ic_std > 0 else 0.0
    
    # IC 衰减: 不同前瞻窗口的 IC
    ic_decay = {}
    for horizon in decay_horizons:
        horizon_ics = []
        for i, d in enumerate(dates):
            if i + horizon >= len(dates):
                break
            fv = factor_values.loc[d]
            future_date = dates[i + horizon]
            if future_date not in forward_returns.index.get_level_values(0):
                continue
            fr = forward_returns.loc[future_date]
            ic = rank_ic(fv, fr)
            if not np.isnan(ic):
                horizon_ics.append(ic)
        if horizon_ics:
            ic_decay[horizon] = float(np.mean(horizon_ics))
        else:
            ic_decay[horizon] = np.nan
    
    return FactorStats(
        name=factor.name,
        rank_ic_mean=ic_mean,
        rank_ic_std=ic_std,
        ic_ir=ic_ir,
        ic_decay=ic_decay,
        n_periods=len(ic_arr),
    )


def compute_ic_series(
    factor_values: pd.Series,
    forward_returns: pd.Series,
) -> pd.Series:
    """计算时间序列 IC: 每个截面一个 IC 值。
    
    返回: Series(index=date, value=Rank_IC)
    """
    ics = {}
    for d in factor_values.index.get_level_values(0).unique():
        fv = factor_values.loc[d]
        if d not in forward_returns.index.get_level_values(0):
            continue
        fr = forward_returns.loc[d]
        ic = rank_ic(fv, fr)
        if not np.isnan(ic):
            ics[d] = ic
    return pd.Series(ics, name="rank_ic").sort_index()


def factor_correlation(factor_values: dict) -> pd.DataFrame:
    """因子截面相关性矩阵 — Spearman。
    
    factor_values: {name: MultiIndex(date,symbol) Series}
    返回: DataFrame(index=factor_name, columns=factor_name), 对角为 1.0
    
    用于检测冗余因子: 相关性 > 0.7 的因子可合并。
    来源: ② Qian, Hua, Sorensen (2007) — 因子相关性管理。
    """
    names = list(factor_values.keys())
    if len(names) < 2:
        return pd.DataFrame([[1.0]], index=names, columns=names)
    
    # 找共同截面
    all_dates = None
    for name in names:
        dates = set(factor_values[name].index.get_level_values(0).unique())
        if all_dates is None:
            all_dates = dates
        else:
            all_dates &= dates
    
    if not all_dates:
        return pd.DataFrame(np.eye(len(names)), index=names, columns=names)
    
    # 在每个截面上计算相关系数, 然后取均值
    corr_sum = np.zeros((len(names), len(names)))
    n_dates = 0
    
    for d in sorted(all_dates):
        series_list = []
        for name in names:
            fv = factor_values[name].loc[d]
            series_list.append(fv)
        # 构建截面矩阵
        df = pd.concat(series_list, axis=1, keys=names).dropna()
        if len(df) < 30:
            continue
        corr = df.corr(method="spearman")
        corr_sum += corr.values
        n_dates += 1
    
    if n_dates == 0:
        return pd.DataFrame(np.eye(len(names)), index=names, columns=names)
    
    avg_corr = corr_sum / n_dates
    return pd.DataFrame(avg_corr, index=names, columns=names)


def factor_report(
    factor_values: dict,
    forward_returns: pd.Series,
    decay_horizons: Optional[list] = None,
) -> dict:
    """生成完整的因子评估报告。
    
    返回: {
        "factors": {name: FactorStats},
        "correlation": DataFrame,
        "ranking": [{name, ic_mean, ic_ir}] 按 IC_IR 降序
    }
    """
    from factor.base import Factor
    
    if decay_horizons is None:
        from config.loader import get as cfg
        decay_horizons = cfg("factor.decay_horizons", [1, 5, 20])
    
    # 为每个因子创建临时 Factor 实例用于评估
    factor_stats = {}
    for name in factor_values:
        dummy = type("DummyFactor", (Factor,), {"name": name, "category": ""})()
        stats = evaluate_factor(dummy, factor_values[name], forward_returns, decay_horizons)
        factor_stats[name] = stats
    
    corr_matrix = factor_correlation(factor_values)
    
    # 按 IC_IR 降序排名
    ranking = sorted(
        factor_stats.values(),
        key=lambda s: abs(s.ic_ir),
        reverse=True,
    )
    
    return {
        "factors": factor_stats,
        "correlation": corr_matrix,
        "ranking": [
            {"name": r.name, "ic_mean": r.rank_ic_mean, "ic_ir": r.ic_ir}
            for r in ranking
        ],
    }
