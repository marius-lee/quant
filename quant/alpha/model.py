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
        from quant.alpha.synth import sleeve_compose, ic_weighted, equal_weight, intersection_alpha

        if self.combine_mode == "sleeve":
            # IC filtering: drop factors with IC <= 0 (maintains independent sub-portfolios per ADR 017)
            # Handles both {name: {ic_mean, ...}} (from compute_ic) and {name: float} (from DB)
            if ic_map:
                def _ic_ok(name):
                    v = ic_map.get(name, {})
                    if isinstance(v, dict):
                        ic_mean = v.get("ic_mean", 0)
                        if ic_mean <= 0:
                            return False
                        # Grinold & Kahn (1999) Ch.6, Eq.6.16: w_k ∝ IC_k / σ²_k
                        # Monitoring 因子权重按 |IC_5d| / |IC_60d| 连续比例衰减
                        # 无地板 — 状态机在 10d 持续衰减后自动退役因子
                        # Source: Active Portfolio Management, 2nd ed., p.178
                        status = v.get("status", "active")
                        if status == "probation":
                            ic_5d = v.get("ic_5d", v.get("ic_mean", 0))
                            ic_60d = v.get("ic_60d", v.get("ic_mean", 0))
                            if abs(ic_60d) > 1e-5:
                                decay = min(1.0, abs(ic_5d) / abs(ic_60d))
                                return ic_mean * decay > 0
                        return True
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

        # ── ML model modes (lgb / xgb) ──
        # ADR-035 Phase 2: LightGBM / XGBoost 非线性 alpha 预测。
        # 用已训练的 ML 模型将因子截面值映射为预期收益。
        # 未训练/未安装时自动回退到 ic_weighted。
        if self.combine_mode in ("lgb", "xgb"):
            try:
                if self.combine_mode == "lgb":
                    from quant.alpha.qlib_model import get_lgb_model
                    ml_model = get_lgb_model(auto_load=True)
                    ml_name = "lgb"
                else:
                    from quant.alpha.xgb_model import get_xgb_model
                    ml_model = get_xgb_model(auto_load=True)
                    ml_name = "xgb"

                if ml_model.is_trained:
                    alpha_raw = ml_model.predict(factor_values)
                    _log.info(
                        "%s: %d features → %d stocks (IC=%.4f)",
                        ml_name,
                        len(ml_model.feature_names),
                        alpha_raw.notna().sum(),
                        ml_model.metadata.ic_mean if ml_model.metadata else 0,
                    )
                    return alpha_raw
                else:
                    _log.info("%s: model not trained, falling back to ic_weighted", ml_name)
                    return ic_weighted(factor_values, ic_map) if ic_map else equal_weight(factor_values)
            except ImportError:
                _log.info("%s: package not installed, falling back to ic_weighted", self.combine_mode)
                return ic_weighted(factor_values, ic_map) if ic_map else equal_weight(factor_values)
            except Exception as _ml_err:
                _log.warning("%s: predict failed (%s), falling back to ic_weighted",
                             self.combine_mode, _ml_err)
                return ic_weighted(factor_values, ic_map) if ic_map else equal_weight(factor_values)

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
        alpha = alpha_raw.copy()
        # ALG1: sigmoid soft cutoff — smooth transition instead of hard quadratic decay.
        # α' = α / (1 + exp(-k × (α - threshold)))
        # k (steepness) from config; default 10.0 balances selectivity vs smoothness.
        from quant.config.constants import _require_cfg
        k = float(_require_cfg("alpha.sigmoid_steepness"))
        alpha = alpha / (1.0 + np.exp(-k * (alpha - threshold)))
        return alpha
