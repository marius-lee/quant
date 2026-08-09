"""Alpha 层 — 因子合成 + 排名 + 候选池选择 + 模型注册 + 策略注册表.

提供:
  AlphaModel      — 因子合成 + soft cutoff 排名主类
  AlphaStrategy   — 策略抽象基类
  register_alpha  — 策略装饰器注册
  get_alpha       — 策略工厂获取
  list_alphas     — 列出已注册策略
  is_registered   — 检查策略是否已注册
  equal_weight    — 等权平均
  ic_weighted     — IC 加权 (|IC| 比例)
  sleeve_compose  — 分仓合成 (每因子独立选 top N)
  intersection_alpha — 交集筛选
  strict_intersection — 严格交集
"""
from quant.alpha.model import AlphaModel
from quant.alpha.synth import equal_weight, ic_weighted, sleeve_compose, intersection_alpha, strict_intersection
from quant.alpha.strategy import AlphaStrategy, register_alpha, get_alpha, list_alphas, is_registered

__all__ = [
    "AlphaModel",
    "AlphaStrategy",
    "register_alpha",
    "get_alpha",
    "list_alphas",
    "is_registered",
    "equal_weight",
    "ic_weighted",
    "sleeve_compose",
    "intersection_alpha",
    "strict_intersection",
]
