"""Alpha Model — 因子合成 + 排名 + 候选池选择.

将原来散布在 pipeline.py Step 3 和 factor/synth.py 的 Alpha 逻辑
统一封装为 AlphaModel 类, 使 pipeline.py 成为纯粹的编排器.

遵循 config.yaml 单一真相源: 所有参数通过 cfg() 读取, 构造函数仅存实例快照.
"""

import pandas as pd
from config.constants import _require_cfg
from config.loader import get as _cfg
from utils.logger import get_logger

_log = get_logger("alpha.model")


class AlphaModel:
    """因子合成 + 软截断排名.

    combine_mode:
      "sleeve"  — 每因子独立分仓, 取并集 (sleeve_compose)
      "composite" — 加权压缩为单一得分 (ic_weighted / equal_weight / intersection)

    所有参数读取自 config.yaml, 构造函数参数为可选覆盖.
    """

    def __init__(self, combine_mode=None, method=None, top_fraction=None,
                 positions_per_factor=None, min_factors=None, intersection_primary=None,
                 intersection_top_fraction=None):
        self.combine_mode = combine_mode or _require_cfg("alpha.combine_mode")
        self._method = method or _require_cfg("alpha.method")
        self.top_fraction = top_fraction or _require_cfg("alpha.top_fraction")
        self.positions_per_factor = positions_per_factor or _require_cfg("alpha.sleeve.positions_per_factor")
        self.min_factors = min_factors or _require_cfg("alpha.sleeve.min_factors")
        self.intersection_primary = intersection_primary or _require_cfg("alpha.intersection_primary")
        self.intersection_top_fraction = intersection_top_fraction or _require_cfg("alpha.intersection_top_fraction")

    def combine(self, factor_values, ic_map=None):
        """将多个因子合成为单一 alpha score.

        factor_values: {name: Series(index=symbol)} — 同日期截面的因子值
        ic_map: {name: weight} — IC 权重 (仅 ic_weighted 模式使用)

        返回: Series(index=symbol), 合成得分
        """
        from alpha.synth import sleeve_compose, ic_weighted, equal_weight, intersection_alpha

        if self.combine_mode == "sleeve":
            alpha_raw = sleeve_compose(
                factor_values,
                positions_per_factor=self.positions_per_factor,
                min_factors=self.min_factors,
            )
            _log.info("sleeve: %d factors -> %d stocks", len(factor_values), alpha_raw.notna().sum())
            return alpha_raw

        # composite mode
        method = self._method
        if method == "intersection":
            return intersection_alpha(
                factor_values,
                top_fraction=self.intersection_top_fraction,
                primary_factor=self.intersection_primary,
            )
        elif method == "ic_weighted" and ic_map:
            return ic_weighted(factor_values, ic_map)
        else:
            if method == "ic_weighted" and not ic_map:
                _log.info("IC cache unavailable, falling back to equal_weight")
            return equal_weight(factor_values)

    def rank(self, alpha_raw, method_override=None):
        """Soft cutoff: 削弱弱信号 (二次衰减) 而非硬砍.

        intersection 模式跳过 (候选池已由交集决定).
        """
        method = method_override or self._method
        if method == "intersection":
            return alpha_raw.copy()

        if alpha_raw.notna().sum() <= 10:
            return alpha_raw.copy()

        if self.top_fraction >= 1.0:
            return alpha_raw.copy()

        threshold = alpha_raw.quantile(1.0 - self.top_fraction)
        below = alpha_raw < threshold
        alpha = alpha_raw.copy()
        if below.any():
            alpha[below] = alpha[below] * (alpha[below] / threshold) ** 2
        return alpha
