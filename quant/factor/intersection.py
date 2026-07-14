"""交集筛选 Alpha 合成 — 替代 IC 加权。

方法: 股票必须在所有因子排前 X% 才进入候选池，然后选最强动量因子(gap_5d)得分最高的。

来源: 实战经验 — A 股小资金单仓策略, IC 加权把多个中等质量因子稀释成
      一个低质量复合因子。交集法要求股票"各科都优秀"而非"偏科"。
"""

import numpy as np
import pandas as pd
from quant.utils.logger import get_logger

logger = get_logger("factor.intersection")


def intersection_alpha(
    factor_values: dict,
    top_fraction: float = 0.20,
    primary_factor: str = "gap_5d",
) -> pd.Series:
    """交集筛选 Alpha 合成。

    factor_values: {name: Series(index=symbol, value=factor_zscore)}
    top_fraction: 每个因子保留前多少比例的股票 (default 0.20 = top 20%)
    primary_factor: 最终排序因子 (在通过交集筛选的候选中选这个因子最高的)

    返回: Series(index=symbol, value=alpha_score), 未通过交集的股票 alpha=NaN

    算法:
      1. 对每个因子独立 rank → percentile
      2. 只保留所有因子 percentile >= (1 - top_fraction) 的股票
      3. 在交集中按 primary_factor 降序排列
      4. 交集为空时放宽至 top_fraction * 2, 仍为空则以 primary_factor 单一排名

    复杂度: O(N × F) where N=股票数, F=因子数
    """
    if not factor_values:
        return pd.Series(dtype=float)

    names = list(factor_values.keys())
    if len(names) == 0:
        return pd.Series(dtype=float)

    # Step 1: Compute percentile rank for each factor
    percentiles = {}
    threshold = 1.0 - top_fraction

    for name in names:
        fv = factor_values[name].dropna()
        if len(fv) < 30:
            continue
        # 百分位排名: 0=最低, 1=最高
        pct = fv.rank(pct=True)
        percentiles[name] = pct

    if not percentiles:
        return pd.Series(dtype=float)

    # Step 2: Intersection — stock must pass ALL factors
    pct_df = pd.DataFrame(percentiles)
    passes = (pct_df >= threshold).all(axis=1)
    candidates = pct_df[passes].index

    # Step 3: Relax if too few candidates
    if len(candidates) < 5 and top_fraction < 0.50:
        logger.info(f"intersection: only {len(candidates)} candidates at top {top_fraction:.0%}, "
                    f"relaxing to top {top_fraction*2:.0%}")
        return intersection_alpha(factor_values, top_fraction * 2, primary_factor)

    if len(candidates) < 3:
        # Fallback: single-factor ranking on primary factor
        logger.info(f"intersection: falling back to single-factor ({primary_factor})")
        if primary_factor in factor_values:
            fv = factor_values[primary_factor].dropna()
            return fv.rank(ascending=False)
        return pd.Series(dtype=float)

    # Step 4: Among candidates, rank by primary factor
    if primary_factor in factor_values:
        primary = factor_values[primary_factor].reindex(candidates).dropna()
        if len(primary) < 3:
            primary = pd.Series(1.0, index=candidates)
    else:
        primary = pd.Series(1.0, index=candidates)

    # Normalize to z-score-like scale
    result = (primary - primary.mean()) / primary.std(ddof=1)
    result = result.reindex(pct_df.index)  # NaN for non-candidates

    n_total = len(pct_df)
    logger.info(f"intersection: {len(candidates)}/{n_total} candidates ({len(candidates)/n_total*100:.1f}%) "
                f"from top {top_fraction:.0%} on {len(names)} factors")

    return result


def strict_intersection(
    factor_values: dict,
    top_n_per_factor: int = 100,
    primary_factor: str = "gap_5d",
) -> pd.Series:
    """严格交集: 每个因子取 top N 只股票, 必须同时出现在 ALL 因子的 top N 中。

    比 percentile 更严格 — 不管总共有多少股票, 每个因子只取固定的 top N。

    返回: Series(index=symbol, value=alpha)
    """
    if not factor_values:
        return pd.Series(dtype=float)

    names = list(factor_values.keys())
    if len(names) == 0:
        return pd.Series(dtype=float)

    # Top N per factor
    top_sets = []
    for name in names:
        fv = factor_values[name].dropna()
        if len(fv) < top_n_per_factor:
            top_sets.append(set(fv.index))
        else:
            top_sets.append(set(fv.nlargest(top_n_per_factor).index))

    # Intersection
    candidates = top_sets[0]
    for s in top_sets[1:]:
        candidates = candidates & s

    if len(candidates) < 3:
        logger.info(f"strict_intersection: only {len(candidates)} candidates, "
                    f"relaxing to union of top {top_n_per_factor}")
        candidates = set()
        for s in top_sets:
            candidates = candidates | s

    # Rank by primary factor
    if primary_factor in factor_values:
        primary = factor_values[primary_factor].reindex(list(candidates)).dropna()
    else:
        primary = pd.Series(1.0, index=list(candidates))

    result = (primary - primary.mean()) / primary.std(ddof=1)
    logger.info(f"strict_intersection: {len(candidates)} candidates from "
                f"top {top_n_per_factor} × {len(names)} factors")
    return result
