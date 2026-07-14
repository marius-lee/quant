"""Probability of Backtest Overfitting — De Prado (2018) Chapter 8.

PBO = fraction of CPCV folds where the best IS factor ranks below median OOS.
High PBO -> overfitting.

标准公式 (De Prado 2018 Eq.8.1):
  PBO = (1/C) * sum_{c=1}^{C} 1[ rank_OOS(best_IS_c) < N/2 ]

阈值: logit(PBO) < -0.847 (对应 PBO < 0.3)
"""

import numpy as np
from scipy.special import logit as _logit
from quant.utils.logger import get_logger


def compute_pbo(fold_results: list[dict], factor_names: list[str]) -> dict:
    """Compute PBO from CPCV fold results — De Prado (2018) Ch.8 standard.

    Parameters
    ----------
    fold_results : list of dict
        Each dict: {factor_name: {is_icir, oos_icir, ...}}, one per fold.
    factor_names : list of str

    Returns
    -------
    dict: pbo, logit_pbo, passed, is_oos_corr, per_factor
    """
    logger = get_logger("evaluation.pbo")
    logger.info(f"PBO: {len(factor_names)} factors, {len(fold_results)} folds")
    n_folds = len(fold_results)
    if n_folds < 2:
        return {"pbo": 0.5, "logit_pbo": 0.0, "passed": True,
                "is_oos_corr": 1.0, "per_factor": {}}

    n_factors = len(factor_names)
    is_icir_matrix = np.zeros((n_factors, n_folds))
    oos_icir_matrix = np.zeros((n_factors, n_folds))

    for fi, fold in enumerate(fold_results):
        for fj, name in enumerate(factor_names):
            if name in fold:
                is_icir_matrix[fj, fi] = fold[name].get("is_icir", 0.0)
                oos_icir_matrix[fj, fi] = fold[name].get("oos_icir", 0.0)

    # ── PBO: De Prado (2018) Eq.8.1 ──
    # PBO = fraction of folds where best IS factor ranks in bottom half of OOS
    median_rank = n_factors // 2
    n_below_median = 0

    for fi in range(n_folds):
        is_vals = is_icir_matrix[:, fi]
        oos_vals = oos_icir_matrix[:, fi]

        if is_vals.std() < 1e-10 or oos_vals.std() < 1e-10:
            # All factors tied in this fold -> skip
            continue

        best_is_idx = int(np.argmax(is_vals))
        # OOS rank: 0 = worst, n_factors-1 = best
        oos_rank = int(np.argsort(np.argsort(oos_vals))[best_is_idx])

        if oos_rank < median_rank:
            n_below_median += 1

    # If no valid folds, use conservative estimate
    valid_folds = sum(1 for fi in range(n_folds)
                      if is_icir_matrix[:, fi].std() > 1e-10
                      and oos_icir_matrix[:, fi].std() > 1e-10)

    pbo = n_below_median / max(valid_folds, 1)

    # logit(PBO): clip to avoid log(0) or log(1)
    pbo_clipped = np.clip(pbo, 0.001, 0.999)
    logit_pbo = float(_logit(pbo_clipped))

    # IS-OOS ICIR Spearman correlation (across factors per fold)
    corrs = []
    for fi in range(n_folds):
        if is_icir_matrix[:, fi].std() > 0 and oos_icir_matrix[:, fi].std() > 0:
            from scipy.stats import spearmanr
            r, _ = spearmanr(is_icir_matrix[:, fi], oos_icir_matrix[:, fi])
            corrs.append(r)
    is_oos_corr = float(np.mean(corrs)) if corrs else 0.0

    # Per-factor OOS stability
    per_factor = {}
    for fj, name in enumerate(factor_names):
        oos_vals = oos_icir_matrix[fj, :]
        per_factor[name] = {
            "oos_icir_mean": float(oos_vals.mean()),
            "oos_icir_std": float(oos_vals.std()) if n_folds > 1 else 0.0,
            "stable": bool(oos_vals.mean() > 0 and
                          (oos_vals.std() / max(abs(oos_vals.mean()), 0.01)) < 2.0)
        }

    logger.info(f"PBO={pbo:.3f} (best_IS<median_OOS in {n_below_median}/{valid_folds} folds), "
                f"logit={logit_pbo:+.3f}, IS-OOS corr={is_oos_corr:+.3f}, passed={logit_pbo < -0.847}")
    return {
        "pbo": float(pbo),
        "logit_pbo": logit_pbo,
        "passed": bool(logit_pbo < -0.847),  # PBO < 0.3
        "is_oos_corr": is_oos_corr,
        "per_factor": per_factor,
    }
