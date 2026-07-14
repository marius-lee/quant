"""G3: Deflated Sharpe Ratio + MinTRL — De Prado (2018) Ch.7-8.

DSR = PSR(SR_0) where SR_0 is the expected max SR after M independent trials.
MinTRL = minimum track record length for statistical significance.

来源: Bailey & Lopez de Prado (2014) JPM; De Prado (2018) AFML pp.105-109.
"""
import numpy as np
from scipy.stats import norm
from utils.logger import get_logger

_log = get_logger("evaluation.deflated_sharpe")


def probabilistic_sharpe_ratio(
    observed_sr: float,
    sr_benchmark: float,
    n_obs: int,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """PSR: Probabilistic Sharpe Ratio — De Prado (2018) Eq.7.2.

    PSR estimates the probability that the true SR exceeds a given benchmark.
    PSR > 0.95 → statistically significant at 95% confidence.

    Args:
        observed_sr: annualized Sharpe ratio from backtest
        sr_benchmark: benchmark SR (0 for "beats cash", or expected max SR)
        n_obs: number of observations (trading days)
        skewness: return distribution skewness (A股典型: -0.5)
        kurtosis: return distribution excess kurtosis + 3 (A股典型: 5-8)

    Returns: PSR probability (0-1)
    """
    if n_obs < 2:
        return 0.0

    se = np.sqrt(
        (1 - skewness * observed_sr + (kurtosis - 1) / 4 * observed_sr**2) / n_obs
    )
    if se <= 0:
        return 0.5

    z_stat = (observed_sr - sr_benchmark) / max(se, 1e-10)
    return float(norm.cdf(z_stat))


def expected_max_sr(n_trials: int, n_obs: int, skewness: float = 0.0, kurtosis: float = 3.0) -> float:
    """Expected maximum Sharpe ratio from M independent trials — De Prado (2018) Eq.7.1.

    Under the null (no skill), this is the SR you'd get from pure luck
    after trying M different strategies/parameter combinations.

    Args:
        n_trials: number of independent strategy trials
        n_obs: number of observations per trial

    Returns: expected max SR under null
    """
    if n_trials <= 0 or n_obs < 2:
        return 0.0

    gamma = 0.5772156649

    base = np.sqrt(2 * np.log(max(n_trials, 1))) / np.sqrt(n_obs)
    correction = (gamma * (1 - skewness * base + (kurtosis - 1) / 4 * base**2))
    return float(base + correction / np.sqrt(n_obs))


def deflated_sharpe_ratio(
    observed_sr: float,
    n_trials: int,
    n_obs: int,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> dict:
    """DSR: Deflated Sharpe Ratio — Bailey & Lopez de Prado (2014) JPM.

    DSR = PSR(SR_0) where SR_0 = E[max_SR] under null of no skill.
    This corrects for multiple testing bias.

    DSR > 0.95 → statistically significant after correcting for M trials.

    Returns dict with dsr, expected_max_sr, is_significant, psr
    """
    if n_obs < 2 or n_trials <= 0:
        return {"dsr": 0.0, "expected_max_sr": 0.0, "is_significant": False, "psr": 0.0}

    sr_0 = expected_max_sr(n_trials, n_obs, skewness, kurtosis)
    psr_val = probabilistic_sharpe_ratio(observed_sr, sr_0, n_obs, skewness, kurtosis)

    _log.info(
        f"DSR: observed_SR={observed_sr:.3f}, E[max_SR|M={n_trials},T={n_obs}]={sr_0:.3f}, "
        f"PSR={psr_val:.3f}, significant={psr_val > 0.95}"
    )

    return {
        "dsr": round(psr_val, 4),
        "expected_max_sr": round(sr_0, 4),
        "is_significant": bool(psr_val > 0.95),
        "psr": round(psr_val, 4),
    }


def min_track_record_length(
    target_sr: float = 0.5,
    sr_benchmark: float = 0.0,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
    confidence: float = 0.95,
) -> float:
    """MinTRL: Minimum Track Record Length — De Prado (2018) Eq.7.3.

    How many observations are needed for the observed SR to be
    statistically significant at given confidence level?

    MinTRL ≈ 1 + [1 - skewness*SR + (kurtosis-1)/4 * SR^2] * (Z_alpha/SR)^2

    Args:
        target_sr: annualized SR you want to prove
        sr_benchmark: benchmark SR (0 = just beats cash)
        confidence: desired confidence level (0.95 = 95%)

    Returns: minimum number of observations (trading days)
    """
    if target_sr <= 0:
        return float("inf")

    z_alpha = norm.ppf(confidence)

    factor = (1 - skewness * (target_sr - sr_benchmark)
              + (kurtosis - 1) / 4 * (target_sr - sr_benchmark)**2)
    min_obs = 1 + factor * (z_alpha / (target_sr - sr_benchmark))**2

    return float(np.ceil(min_obs))


def compute_dsr_for_strategy(
    daily_returns: list[float],
    n_factors: int = 30,
    annual_factor: float = 252.0,
    skewness: float = -0.5,
    kurtosis: float = 8.0,
) -> dict:
    """便捷函数: 从日收益序列计算 DSR + MinTRL.

    Args:
        daily_returns: list of daily PnL returns
        n_factors: number of factors tested (用于估计 M = independent trials)
        annual_factor: trading days per year (A股 = 252)
        skewness: A股 return skewness (典型 -0.5)
        kurtosis: A股 return kurtosis (典型 6-8)

    Returns: {dsr, min_trl_years, annualized_sr, n_obs}
    """
    if len(daily_returns) < 20:
        return {"dsr": 0.0, "min_trl_years": float("inf"), "annualized_sr": 0.0, "n_obs": len(daily_returns)}

    rets = np.array(daily_returns)
    annualized_sr = float(np.mean(rets) / max(np.std(rets, ddof=1), 1e-10) * np.sqrt(annual_factor))
    n_obs = len(rets)

    n_trials = max(1, n_factors * 2)

    dsr_result = deflated_sharpe_ratio(annualized_sr, n_trials, n_obs, skewness, kurtosis)
    min_trl_days = min_track_record_length(annualized_sr, 0, skewness, kurtosis)
    min_trl_years = round(min_trl_days / annual_factor, 2)

    return {
        "dsr": dsr_result["dsr"],
        "expected_max_sr": dsr_result["expected_max_sr"],
        "is_significant": dsr_result["is_significant"],
        "min_trl_years": min_trl_years,
        "annualized_sr": round(annualized_sr, 4),
        "n_obs": n_obs,
    }
