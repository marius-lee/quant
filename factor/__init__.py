"""因子层 — Layer 2: 因子计算 + 评估 + 合成。

模块:
  base.py     — Factor 抽象基类, FactorResult, FactorStats
  compute.py  — 6 类 11 个因子 (动量/反转/波动率/成交量/Amihud/偏度)
  evaluate.py — 截面 Rank IC, IC_IR, IC 衰减, 相关性矩阵
  synth.py    — 等权/IC 加权合成
"""

from factor.base import Factor, FactorResult, FactorStats
from factor.compute import (
    compute_momentum,
    compute_reversal,
    compute_volatility,
    compute_downside_volatility,
    compute_volume_ratio,
    compute_turnover_change,
    compute_amihud,
    compute_skewness,
    compute_all_factors,
    get_factor_names,
    FACTOR_REGISTRY,
)
from factor.evaluate import (
    rank_ic,
    evaluate_factor,
    compute_ic_series,
    factor_correlation,
    factor_report,
)
from factor.synth import equal_weight, ic_weighted, synthesize

__all__ = [
    "Factor", "FactorResult", "FactorStats",
    "compute_momentum", "compute_reversal", "compute_volatility",
    "compute_downside_volatility", "compute_volume_ratio",
    "compute_turnover_change", "compute_amihud", "compute_skewness",
    "compute_all_factors", "get_factor_names", "FACTOR_REGISTRY",
    "rank_ic", "evaluate_factor", "compute_ic_series",
    "factor_correlation", "factor_report",
    "equal_weight", "ic_weighted", "synthesize",
]
