"""因子合成 — 将多个因子合成为单一复合因子得分。

导出:
  equal_weight        — 等权平均
  ic_weighted         — IC 加权 (|IC| 比例)
  intersection_alpha  — 交集筛选 (每因子排前 X% 才进候选池)
  strict_intersection — 严格交集 (每因子取 top N, 同时出现才进池)

合成方法:
  equal_weight — 等权平均, 简单但忽略因子质量差异
  ic_weighted  — IC 加权 (|IC| 比例), 给预测力强的因子更高权重

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
    # 等权: 只取有效值, 按行平均
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

    # 权重: 带符号 IC 归一化
    raw_weights = np.array([ic_scores[n] for n in names])
    total = np.abs(raw_weights).sum()
    if total == 0:
        return equal_weight(factor_values)
    weights = raw_weights / total

    # 每列 z-score 并截断
    df = pd.DataFrame({n: factor_values[n] for n in names})
    for col in df.columns:
        mu = df[col].mean()
        sigma = df[col].std(ddof=1)
        if sigma == 0:
            df[col] = 0
            continue
        z = (df[col] - mu) / sigma
        df[col] = z.clip(-clip, clip)

    # 加权求和
    composite = (df * weights).sum(axis=1)
    return composite
