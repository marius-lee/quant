"""CPCV + DSR 因子健康评估 — 替代单切分 OOS/IS 短窗判定 (§8.4, test-v300).

动机: attribution 旧 L2 用单切分 train 250d/test 20d 的 OOS/IS IR 比率,
统计功效低且强依赖切分点 (报告 §8.4 "短窗判定")。改为:
  1. Purged Walk-Forward CPCV (De Prado 2018 Ch.7) 切 fold, 各 fold 只取
     OOS (test) 段, 拼接为纯样本外 IC 序列;
  2. OOS 序列 → 日频 ICIR (mean/std) 作为 observed SR — DSR/PSR 公式内
     E[max_SR] 与标准误同为 per-period 口径, 传年化值会膨胀 √244 倍
     导致全显著 (test-v300 修正); 年化 ICIR 另作展示字段;
  3. DSR (Bailey & Lopez de Prado 2014): 以评估因子总数为试验数 M 做
     多重检验校正, 偏度/峰度取 OOS 序列实测。

verdict 语义 (对齐 Phase 3 因子准入):
  insufficient — OOS 天数 < cpcv_min_days, 不罚 (新因子/数据不足)
  degraded     — DSR < dsr_degraded_threshold, 更像运气 → active 降 monitoring
  significant  — DSR ≥ dsr_recover_threshold, 统计显著 → monitoring 恢复候选
  neutral      — 介于两阈值之间, 不动作
"""

import numpy as np
import pandas as pd
from scipy.stats import skew, kurtosis

from quant.config.constants import _require_cfg
from quant.evaluation.cpcv import PurgedWalkForward
from quant.evaluation.deflated_sharpe import deflated_sharpe_ratio
from quant.utils.logger import get_logger

_log = get_logger("evaluation.cpcv_dsr")


def cpcv_oos_series(ic_values, n_groups: int = None, embargo_days: int = None) -> pd.Series:
    """日频 IC 序列 → CPCV 各 fold OOS 段拼接的纯样本外 IC 序列。

    Args:
        ic_values: 日频 IC 值 (list/np.ndarray/pd.Series), 时间升序, 可含 NaN。
        n_groups: CPCV 组数 (None=读 factor.evaluation.cpcv_groups)。
        embargo_days: 各 fold 训练段尾部隔离天数 (None=读 factor.evaluation.embargo_days)。

    Returns:
        pd.Series: 按时间升序拼接的 OOS IC 序列; 首个 fold 无训练段故不含
        (walk-forward 约束); 输入 < 4 个有效点时为空 Series。
    """
    if n_groups is None:
        n_groups = _require_cfg("factor.evaluation.cpcv_groups")
    if embargo_days is None:
        embargo_days = _require_cfg("factor.evaluation.embargo_days")

    s = pd.Series(ic_values).dropna().reset_index(drop=True)
    if len(s) < 4:
        return pd.Series(dtype=float)

    pvf = PurgedWalkForward(n_groups=n_groups, embargo_days=embargo_days)
    splits = pvf.split(list(s.index))
    oos_idx = sorted({i for _, test_idx in splits for i in test_idx})
    out = s.iloc[oos_idx]
    _log.debug("CPCV: %d IC days → %d folds → %d OOS days", len(s), len(splits), len(out))
    return out


def evaluate_factor(ic_values, n_trials: int, min_days: int = None,
                    degraded_threshold: float = None, recover_threshold: float = None,
                    n_groups: int = None, embargo_days: int = None) -> dict:
    """单因子 CPCV+DSR 健康判定。

    Args:
        ic_values: 日频 IC 序列, 时间升序 (factor_ic_daily live scope)。
        n_trials: 参与评估的因子总数 M (多重检验校正)。
        min_days: OOS 序列最少天数, 不足 → insufficient (None=读 attribution.cpcv_min_days)。
        degraded_threshold: DSR < 此值 → degraded (None=读 attribution.dsr_degraded_threshold)。
        recover_threshold: DSR ≥ 此值 → significant (None=读 attribution.dsr_recover_threshold)。
        n_groups/embargo_days: 透传 cpcv_oos_series。

    Returns:
        dict: {verdict, dsr, oos_icir (年化展示值), expected_max_sr, n_obs,
        skewness, kurtosis}; insufficient 时 dsr/oos_icir 为 None。
    """
    if min_days is None:
        min_days = _require_cfg("attribution.cpcv_min_days")
    if degraded_threshold is None:
        degraded_threshold = _require_cfg("attribution.dsr_degraded_threshold")
    if recover_threshold is None:
        recover_threshold = _require_cfg("attribution.dsr_recover_threshold")

    oos = cpcv_oos_series(ic_values, n_groups, embargo_days)
    if len(oos) < min_days:
        _log.debug("CPCV+DSR: only %d OOS days (< %d) → insufficient", len(oos), min_days)
        return {"verdict": "insufficient", "dsr": None, "oos_icir": None,
                "expected_max_sr": None, "n_obs": len(oos),
                "skewness": None, "kurtosis": None}

    mu = float(oos.mean())
    sd = float(oos.std(ddof=1))
    annual = float(_require_cfg("market.annual_trading_days"))
    icir_daily = mu / sd if sd > 0 else 0.0
    oos_icir_ann = icir_daily * np.sqrt(annual)
    sk = float(skew(oos))
    ku = max(float(kurtosis(oos, fisher=False)), 1.0)  # PSR 公式要求峰度 ≥ 1 防负方差项

    # 单位一致: observed SR 与 E[max_SR] 均为 per-period (日频) 口径
    r = deflated_sharpe_ratio(icir_daily, n_trials=max(int(n_trials), 1),
                              n_obs=len(oos), skewness=sk, kurtosis=ku)
    dsr = r["dsr"]
    if dsr < degraded_threshold:
        verdict = "degraded"
    elif dsr >= recover_threshold:
        verdict = "significant"
    else:
        verdict = "neutral"

    _log.info("CPCV+DSR: OOS n=%d, ICIR(daily)=%+.4f (ann %+.3f), DSR=%.4f (M=%d, E[max_SR]=%.3f) → %s",
              len(oos), icir_daily, oos_icir_ann, dsr, n_trials, r["expected_max_sr"], verdict)
    return {"verdict": verdict, "dsr": dsr, "oos_icir": round(oos_icir_ann, 4),
            "expected_max_sr": r["expected_max_sr"], "n_obs": len(oos),
            "skewness": round(sk, 3), "kurtosis": round(ku, 3)}
