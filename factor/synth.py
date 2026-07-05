"""因子合成 — 将多个因子合成为单一复合因子得分。

导出:
  equal_weight        — 等权平均
  ic_weighted         — IC 加权 (|IC| 比例)
  sleeve_compose      — 分仓合成 (每因子独立选 top N, 取并集)
  intersection_alpha  — 交集筛选 (每因子排前 X% 才进候选池)
  strict_intersection — 严格交集 (每因子取 top N, 同时出现才进池)

合成模式 (config.yaml alpha.combine_mode):
  composite — 加权压缩为单一得分 (ic_weighted / equal_weight / intersection)
  sleeve    — 每因子独立分仓, 保留因子间独立信号 (sleeve_compose)

来源: ② Grinold & Kahn (2000) Chapter 8 — Alpha 合成.
"""

import numpy as np
import pandas as pd
from factor.intersection import intersection_alpha, strict_intersection


def equal_weight(factor_values: dict) -> pd.Series:
    """等权合成: 所有因子取 z-score 后等权平均。

    factor_values: {name: Series(index=symbol)} — 同日期截面的因子值
    min_factors: 至少需要的有效因子数 (默认 len//2)

    返回: Series(index=symbol), 合成得分
    来源: ② 最朴素的合成方式, 当 IC 估计不可靠时的安全选择
    """
    names = list(factor_values.keys())
    if not names:
        return pd.Series(dtype=float)

    composite = pd.DataFrame(factor_values)
    min_factors = max(1, len(names) // 2)
    composite = composite.dropna(thresh=min_factors)
    return composite.mean(axis=1)


def ic_weighted(
    factor_values: dict,
    ic_scores: dict,
    clip: float = 3.0,
) -> pd.Series:
    """IC 加权合成: 权重 ∝ |IC|。

    factor_values: {name: Series(index=symbol)}
    ic_scores: {name: IC 值} — 从 FactorStats.rank_ic_mean 获取
    clip: z-score 截断阈值, 防止极端因子值主导合成

    返回: Series(index=symbol)
    来源: ② Grinold & Kahn (2000) — IC 加权 alpha 合成
    """
    names = [n for n in factor_values if n in ic_scores]
    if not names:
        return equal_weight(factor_values)

    raw_weights = np.array([ic_scores[n] for n in names])
    total = np.abs(raw_weights).sum()
    if total == 0:
        return equal_weight(factor_values)
    weights = raw_weights / total

    df = pd.DataFrame({n: factor_values[n] for n in names})
    for col in df.columns:
        mu = df[col].mean()
        sigma = df[col].std(ddof=1)
        if sigma == 0:
            df[col] = 0
            continue
        z = (df[col] - mu) / sigma
        df[col] = z.clip(-clip, clip)

    composite = (df * weights).sum(axis=1)
    return composite


import logging
_log = logging.getLogger("quant.factor.synth")

def sleeve_compose(
    factor_values: dict,
    positions_per_factor: int = 8,
    min_factors: int = 1,
) -> pd.Series:
    """分仓合成: 每个因子独立选取 top N 只股票, 取并集。
    返回原始 z-score (保留信号梯度), 不做等权压扁。

    与 composite 模式的本质区别: 不做维度压缩。reversal 选超跌、
    volatility 选低波、momentum 选趋势 — 不同的逻辑不应该被加权冲淡。

    factor_values: {name: Series(index=symbol)} — 已 z-score 的截面因子值
    positions_per_factor: 每个因子选取的股票数
    min_factors: 最少有效因子数 (低于此数返回空)

    返回: Series(index=symbol), 值 = 1.0 (标记入选)
    """
    if len(factor_values) < min_factors:
        _log.debug("sleeve_compose: %d factors < min_factors=%d, returning empty", len(factor_values), min_factors)
        return pd.Series(dtype=float)

    score_map = {}

    for name, scores in factor_values.items():
        valid = scores.dropna()
        cnt = len(valid)
        sel_n = min(positions_per_factor, cnt)
        _log.debug("sleeve: %s → %d/%d valid, picking top %d", name, cnt, len(scores), sel_n)
        if len(valid) == 0:
            continue

        # 取 top N (z-score 高者优先)
        top_n = min(positions_per_factor, cnt)
        top_series = valid.nlargest(top_n)
        for sym, val in top_series.items():
            # 每个因子贡献其原始 z-score; 被多因子同时选中的取最大值
            score_map[sym] = max(score_map.get(sym, -999), val)

    if not score_map:
        _log.warning("sleeve_compose: 0 stocks selected from %d factors", len(factor_values))
        return pd.Series(dtype=float)
    _log.info("sleeve: %d factors → %d stocks (positions_per_factor=%d, score range %.2f~%.2f)",
              len(factor_values), len(score_map), positions_per_factor,
              min(score_map.values()), max(score_map.values()))

    return pd.Series(score_map, name="alpha").sort_values(ascending=False)
