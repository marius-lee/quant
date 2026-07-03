"""因子层 — Layer 2: 因子计算 + 合成 + 缓存。

模块:
  compute.py     — 11 动态 + 5 基本面因子
  synth.py       — equal_weight / ic_weighted 合成
  stats_cache.py — 因子 IC 评估 + 缓存

因子覆盖: 反转、波动率、换手率、极端收益、隔夜缺口、振幅、动量(反转)、偏度、
         特质波动、流动性、北向资金 + EP/BP/ROE/规模/52周
"""

from factor.compute import (
    compute_all_factors,
    get_factor_names,
    load_active_price_factors,
    load_active_fundamental_factors,
)
from factor.synth import equal_weight, ic_weighted

__all__ = [
    "compute_all_factors", "get_factor_names",
    "load_active_price_factors", "load_active_fundamental_factors",
    "equal_weight", "ic_weighted",
]
