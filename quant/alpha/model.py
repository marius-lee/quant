"""Alpha Model — 因子合成 + 排名 + 候选池选择.

将原来散布在 pipeline.py Step 3 和 factor/synth.py 的 Alpha 逻辑
统一封装为 AlphaModel 类, 使 pipeline.py 成为纯粹的编排器.

遵循 config.yaml 单一真相源: 所有参数通过 _require_cfg() 读取 , 构造函数仅存实例快照.
"""

import pandas as pd
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

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
        from quant.alpha.synth import sleeve_compose, ic_weighted, equal_weight, intersection_alpha

        if self.combine_mode == "sleeve":
            # IC filtering: drop factors with IC <= 0 (maintains independent sub-portfolios per ADR 017)
            # Handles both {name: {ic_mean, ...}} (from compute_ic) and {name: float} (from DB)
            if ic_map:
                def _ic_ok(name):
                    v = ic_map.get(name, {})
                    if isinstance(v, dict):
                        return v.get("ic_mean", 0) > 0
                    return v > 0  # plain float from factor_registry
                keep = {k: v for k, v in factor_values.items() if _ic_ok(k)}
                if len(keep) >= self.min_factors:
                    factor_values = keep
                # else: keep all if insufficient factors survive filtering

            alpha_raw = sleeve_compose(
                factor_values,
                positions_per_factor=self.positions_per_factor,
                min_factors=self.min_factors,
            )
            _log.info("sleeve: %d factors -> %d stocks (filtered=%s)", len(factor_values), alpha_raw.notna().sum(), bool(ic_map))
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
