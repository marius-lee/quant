"""因子层 — Layer 2: 因子计算 + 评估 + 合成。

模块:
  base.py     — Factor 抽象基类, FactorResult, FactorStats
  compute.py  — 4 因子 (momentum_10d, volatility_20d, skewness_20d, bp_ratio)
  evaluate.py — 截面 Rank IC, IC_IR, IC 衰减, 相关性矩阵
  synth.py    — 等权/IC 加权合成

因子覆盖 Fama-French 五因子中的动量、低波、偏度、价值 4 个维度。
"""

from factor.base import Factor, FactorResult, FactorStats
from factor.compute import (
    compute_momentum,
    compute_volatility,
    compute_skewness,
    compute_bp_ratio,
    compute_all_factors,
    get_factor_names,
    FACTOR_REGISTRY,
    FUNDAMENTAL_FACTOR_REGISTRY,
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
    "compute_momentum", "compute_volatility", "compute_skewness",
    "compute_bp_ratio",
    "compute_all_factors", "get_factor_names",
    "FACTOR_REGISTRY", "FUNDAMENTAL_FACTOR_REGISTRY",
    "rank_ic", "evaluate_factor", "compute_ic_series",
    "factor_correlation", "factor_report",
    "equal_weight", "ic_weighted", "synthesize",
]
