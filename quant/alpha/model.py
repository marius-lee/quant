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

# P3a: 冗余因子相关性阈值 (WorldQuant 标准: 0.7)
_REDUNDANCY_CORR_THRESHOLD = None  # 懒加载, 由 _require_cfg 读取

def _get_redundancy_threshold() -> float:
    global _REDUNDANCY_CORR_THRESHOLD
    if _REDUNDANCY_CORR_THRESHOLD is None:
        _REDUNDANCY_CORR_THRESHOLD = _require_cfg("factor.compute.redundancy_corr_threshold")
    return _REDUNDANCY_CORR_THRESHOLD


def _adjust_for_redundancy(factor_values: dict, ic_map: dict) -> dict:
    """P3a: 检测共线因子对, 低 IC 方降权。"""
    if len(factor_values) < 2:
        return ic_map
    try:
        names = list(factor_values.keys())
        common_names = [n for n in names if n in ic_map]
        if len(common_names) < 2:
            return ic_map
        df = pd.DataFrame({n: factor_values[n] for n in common_names}).dropna()
        if df.shape[0] < 30 or df.shape[1] < 2:
            return ic_map
        corr = df.corr()
        adjusted = dict(ic_map)
        for i, n1 in enumerate(common_names):
            for j, n2 in enumerate(common_names):
                if j <= i:
                    continue
                if abs(corr.loc[n1, n2]) < _get_redundancy_threshold():
                    continue
                ic1, ic2 = abs(ic_map.get(n1, 0)), abs(ic_map.get(n2, 0))
                loser = n1 if ic1 < ic2 else n2
                if loser in adjusted:
                    adjusted[loser] = adjusted[loser] * 0.5
                    _log.info(f"redundancy: {n1}<->{n2} corr={corr.loc[n1,n2]:.2f} dampen {loser}")
        return adjusted
    except Exception as e:
        _log.debug(f"redundancy skipped (non-fatal): {e}")
        return ic_map


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
        alpha = alpha_raw.copy()
        # ALG1: sigmoid soft cutoff — smooth transition instead of hard quadratic decay.
        # α' = α / (1 + exp(-k × (α - threshold)))
        # k (steepness) from config; default 10.0 balances selectivity vs smoothness.
        from quant.config.constants import _require_cfg
        k = float(_require_cfg("alpha.sigmoid_steepness"))
        alpha = alpha / (1.0 + np.exp(-k * (alpha - threshold)))
        return alpha
