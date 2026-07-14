"""因子层 — Layer 2: 因子计算 + 合成 + 缓存。

模块:
  compute.py     — 动态 + 基本面因子
  synth.py       — equal_weight / ic_weighted 合成
  stats_cache.py — 因子 IC 评估 + 缓存
  registry.py    — 共享工具(z-score, DB, 映射表)
  orchestrator.py— 并行调度

因子覆盖: 反转、波动率、换手率、极端收益、隔夜缺口、振幅、动量(反转)、偏度、
         特质波动、流动性、北向资金 + EP/BP/ROE/规模/52周
"""


def __getattr__(name):
    """Lazy-import — 打破 factor.compute ↔ factor.__init__ 循环依赖。"""
    if name in _compute_exports:
        from quant.factor.compute import (
            compute_all_factors,
            get_factor_names,
            load_active_price_factors,
            load_active_fundamental_factors,
        )
        _result = {
            "compute_all_factors": compute_all_factors,
            "get_factor_names": get_factor_names,
            "load_active_price_factors": load_active_price_factors,
            "load_active_fundamental_factors": load_active_fundamental_factors,
        }
        globals().update(_result)
        return _result[name]
    if name in _synth_exports:
        from quant.factor.synth import (
            equal_weight,
            ic_weighted,
            intersection_alpha,
            strict_intersection,
        )
        _result = {
            "equal_weight": equal_weight,
            "ic_weighted": ic_weighted,
            "intersection_alpha": intersection_alpha,
            "strict_intersection": strict_intersection,
        }
        globals().update(_result)
        return _result[name]
    raise AttributeError(f"module 'factor' has no attribute {name!r}")


_compute_exports = frozenset({
    "compute_all_factors",
    "get_factor_names",
    "load_active_price_factors",
    "load_active_fundamental_factors",
})

_synth_exports = frozenset({
    "equal_weight",
    "ic_weighted",
    "intersection_alpha",
    "strict_intersection",
})

__all__ = sorted(list(_compute_exports | _synth_exports))
