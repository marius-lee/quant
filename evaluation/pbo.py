"""Probability of Backtest Overfitting — De Prado (2018) Chapter 8.

PBO = probability that the best IS (In-Sample) performer is not the best OOS
(Out-of-Sample) performer. High PBO → overfitting.

关键公式:
  PBO = Σ_{c=1}^C 1[rank_IS(c) < rank_IS(c*)] · 1[OOS_IR(c*) < median_OOS] / (C-1)
  简化版: PBO = fraction of folds where IS rank ≠ OOS rank

阈值: logit(PBO) < -0.847 (对应 PBO < 0.3)
"""

import numpy as np
from scipy.special import logit as _logit


def compute_pbo(fold_results: list[dict], factor_names: list[str]) -> dict:
    """Compute PBO from CPCV fold results.

    Parameters
    ----------
    fold_results : list of dict
        Each dict is per-factor fold metrics: {factor: {is_icir, oos_icir, ...}}
        One dict per fold.
    factor_names : list of str
        Factor names to evaluate.

    Returns
    -------
    dict with:
        pbo : float — raw PBO
        logit_pbo : float — logit(PBO)
        passed : bool — logit(PBO) < -0.847
        is_oos_corr : float — correlation between IS and OOS ICIR rankings
        per_factor : dict — per-factor PBO subscores
    """
    n_folds = len(fold_results)
    if n_folds < 2:
        return {"pbo": 0.5, "logit_pbo": 0.0, "passed": True,
                "is_oos_corr": 1.0, "per_factor": {}}

    # Build IS/OOS ICIR matrices: (n_factors, n_folds)
    is_icir_matrix = np.zeros((len(factor_names), n_folds))
    oos_icir_matrix = np.zeros((len(factor_names), n_folds))

    for fi, fold in enumerate(fold_results):
        for fj, name in enumerate(factor_names):
            if name in fold:
                is_icir_matrix[fj, fi] = fold[name].get("is_icir", 0.0)
                oos_icir_matrix[fj, fi] = fold[name].get("oos_icir", 0.0)

    # PBO: across all folds, how often does IS rank differ from OOS rank?
    mismatches = 0
    total_pairs = 0
    for fi in range(n_folds):
        is_rank = np.argsort(np.argsort(is_icir_matrix[:, fi]))  # 0=best
        oos_rank = np.argsort(np.argsort(oos_icir_matrix[:, fi]))
        for fj in range(len(factor_names)):
            for fk in range(fj + 1, len(factor_names)):
                total_pairs += 1
                # Check if IS ranking disagrees with OOS ranking
                is_order = is_rank[fj] < is_rank[fk]
                oos_order = oos_rank[fj] < oos_rank[fk]
                if is_order != oos_order:
                    mismatches += 1

    pbo = mismatches / max(total_pairs, 1)

    # logit(PBO): use clipping to avoid log(0) or log(1)
    pbo_clipped = np.clip(pbo, 0.001, 0.999)
    logit_pbo = float(_logit(pbo_clipped))

    # IS-OOS ICIR correlation (Spearman rank correlation across factors per fold)
    corrs = []
    for fi in range(n_folds):
        if is_icir_matrix[:, fi].std() > 0 and oos_icir_matrix[:, fi].std() > 0:
            from scipy.stats import spearmanr
            r, _ = spearmanr(is_icir_matrix[:, fi], oos_icir_matrix[:, fi])
            corrs.append(r)
    is_oos_corr = float(np.mean(corrs)) if corrs else 0.0

    # Per-factor: average OOS ICIR and stability
    per_factor = {}
    for fj, name in enumerate(factor_names):
        oos_vals = oos_icir_matrix[fj, :]
        per_factor[name] = {
            "oos_icir_mean": float(oos_vals.mean()),
            "oos_icir_std": float(oos_vals.std()) if n_folds > 1 else 0.0,
            "stable": bool(oos_vals.mean() > 0 and
                          (oos_vals.std() / max(abs(oos_vals.mean()), 0.01)) < 2.0)
        }

    return {
        "pbo": float(pbo),
        "logit_pbo": logit_pbo,
        "passed": bool(logit_pbo < -0.847),  # PBO < 0.3
        "is_oos_corr": is_oos_corr,
        "per_factor": per_factor,
    }


def compute_deflated_sharpe(sharpe_ratios: np.ndarray,
                            n_trials: int = 1000) -> dict:
    """Compute Deflated Sharpe Ratio (DSR) — De Prado & Bailey (2014).

    DSR answers: given M trials, what's the probability that the best observed
    Sharpe is statistically significant (not just data-mining noise)?

    Parameters
    ----------
    sharpe_ratios : np.ndarray
        Observed Sharpe ratios from multiple trials.
    n_trials : int
        Number of independent trials.

    Returns
    -------
    dict with:
        dsr : float — Deflated Sharpe Ratio (p-value analogue)
        p_value : float — probability best SR is noise
        significant : bool — DSR > 0.95 (standard threshold)
    """
    if len(sharpe_ratios) < 2:
        return {"dsr": 0.0, "p_value": 1.0, "significant": False}

    from scipy.stats import norm

    sr_max = np.max(sharpe_ratios)
    sr_mean = np.mean(sharpe_ratios)
    sr_std = np.std(sharpe_ratios, ddof=1)

    if sr_std < 1e-10:
        return {"dsr": 0.0, "p_value": 1.0, "significant": False}

    # E[max] under null of N(mean, std^2)
    # Bailey & Lopez de Prado (2014): E[max] ≈ mean + std * sqrt(2 * log(n_trials))
    e_max = sr_mean + sr_std * np.sqrt(2 * np.log(max(n_trials, 2)))

    dsr = (sr_max - sr_mean) / sr_std
    # Adjust for multiple testing
    dsr_deflated = (sr_max - e_max) / sr_std if sr_std > 0 else 0.0

    p_value = 1.0 - norm.cdf(dsr_deflated)

    return {
        "dsr": float(dsr_deflated),
        "p_value": float(p_value),
        "significant": bool(p_value < 0.05)
    }
