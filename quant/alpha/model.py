"""Alpha Model — 因子合成 + 排名 + 候选池选择.

将原来散布在 pipeline.py Step 3 和 factor/synth.py 的 Alpha 逻辑
统一封装为 AlphaModel 类, 使 pipeline.py 成为纯粹的编排器.

遵循 config.yaml 单一真相源: 所有参数通过 _require_cfg() 读取 , 构造函数仅存实例快照.
"""

import numpy as np
import pandas as pd
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

_log = get_logger("alpha.model")

# v534: P3a 冗余降权 (_adjust_for_redundancy) 已删除 — 全项目无调用方,
# 从未生效 (定义于 2026-07 但 combine/combine_regime 均未接线);
# 因子冗余管控由 attribution 每日 factor_redundant 检测 + IC 退化告警承担.


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

        factor_values: {name: Series(index=symbol)}
        ic_map: {name: weight} -- IC 权重 (仅 ic_weighted 模式使用)
        P3a: 自动检测冗余因子 (相关系数 > 0.7 的因子对, 低 IC 方降权).

        返回: Series(index=symbol), 合成得分
        """
        from quant.alpha.strategy import get_alpha, list_alphas, is_registered
        from quant.alpha.synth import ic_weighted, equal_weight

        # ── P2-2 fix: 使用 AlphaStrategy 注册表替代字符串分支 ──
        if not is_registered(self.combine_mode):
            _log.warning(f"AlphaModel: unknown combine_mode '{self.combine_mode}', falling back to ic_weighted")
            return ic_weighted(factor_values, ic_map) if ic_map else equal_weight(factor_values)

        # 实例化策略并执行
        strategy_cls = get_alpha(self.combine_mode)
        strategy = strategy_cls()

        # 准备策略参数
        params = {}
        if self.combine_mode == "sleeve":
            params.update(positions_per_factor=self.positions_per_factor, min_factors=self.min_factors)
        elif self.combine_mode == "intersection":
            params.update(top_fraction=self.intersection_top_fraction, primary_factor=self.intersection_primary)

        # 执行合成
        alpha_raw = strategy.combine(factor_values, ic_map, **params)

        # 记录日志
        _log.info(f"alpha: {self.combine_mode} -> {alpha_raw.notna().sum()} stocks (IC filter={ic_map is not None})")
        return alpha_raw


    def combine_regime(self, factor_values, ic_map=None, regime_label=None, regime_probs=None):
        """Gap 3: Regime-conditional factor combination.

        Boosts factors known to work in the current market regime.
        Falls back to standard combine() if regime info is unavailable.
        """
        if regime_label is None or regime_label == "unknown":
            return self.combine(factor_values, ic_map=ic_map)

        from quant.regime.detector import get_regime_weights
        regime_weights = get_regime_weights(
            list(factor_values.keys()), ic_map, regime_label, regime_probs
        )

        from quant.utils.logger import get_logger
        _rl = get_logger("alpha.model")
        _rl.info(f"regime combine: {regime_label} (confidence={regime_probs.get(regime_label, 0):.2f})")

        return self.combine(factor_values, ic_map=regime_weights)

    def rank(self, alpha_raw, method_override=None):
        """Soft cutoff 已移除 (v534).

        原 ALG1 sigmoid 单调变换 α' = α/(1+exp(-k(α-t))) 对排名选股是
        no-op — 入选集合只由序决定 (top_fraction 截断 + alpha 边际成本
        裁剪均按序), 任何单调变换不改变; 且 sigmoid_steepness=10 无文献
        依据, 权重分配应按原始 alpha 相对差 (组合层 score_weighted),
        不附加拍脑袋非线性。直接返回原始分。

        intersection 模式跳过 (候选池已由交集决定).
        """
        method = method_override or self._method
        if method == "intersection":
            return alpha_raw.copy()

        if alpha_raw.notna().sum() <= 10:
            return alpha_raw.copy()

        if self.top_fraction >= 1.0:
            return alpha_raw.copy()

        return alpha_raw.copy()
