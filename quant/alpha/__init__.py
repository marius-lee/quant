"""Alpha 层 — 因子合成 + 排名 + 候选池选择.

提供:
  AlphaModel      — 因子合成 + soft cutoff 排名主类
  equal_weight    — 等权平均
  ic_weighted     — IC 加权 (|IC| 比例)
  sleeve_compose  — 分仓合成 (每因子独立选 top N)
  intersection_alpha — 交集筛选
"""
from quant.alpha.model import AlphaModel
from quant.alpha.synth import equal_weight, ic_weighted, sleeve_compose
