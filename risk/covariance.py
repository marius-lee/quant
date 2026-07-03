"""协方差估计 — 样本协方差 + Ledoit-Wolf 收缩。

高维截面（~5000 股票 × 60 日）样本协方差不可靠:
  股票数 >> 样本数 → 样本协方差奇异，最小特征值接近 0

Ledoit-Wolf (2004) 收缩估计:
  Σ_shrink = (1 - δ) × Σ_sample + δ × F_target
  其中 δ 为最优收缩强度，F_target 为目标矩阵（常数相关模型）

来源: ② Ledoit & Wolf (2004) — "A well-conditioned estimator for
      large-dimensional covariance matrices"
"""

import numpy as np
import pandas as pd
from typing import Optional


def sample_cov(returns: pd.DataFrame) -> pd.DataFrame:
    """样本协方差矩阵 (ddof=1)。

    returns: DataFrame, index=date, columns=symbols
    返回: DataFrame, index=columns=symbols
    """
    return returns.cov()


def _constant_correlation_target(cov: np.ndarray) -> np.ndarray:
    """构造 Ledoit-Wolf 常数相关目标矩阵。

    所有股票方差相同（均值方差），所有 pairwise 相关系数相同（均值相关）。
    这是一个高度结构化的矩阵，极端条件数好。
    """
    n = cov.shape[0]
    # 均值方差
    avg_var = np.trace(cov) / n
    # 均值相关系数
    std = np.sqrt(np.diag(cov))
    # D⁻¹ × Σ × D⁻¹ 得到相关矩阵
    with np.errstate(divide="ignore", invalid="ignore"):
        inv_std = np.where(std > 0, 1.0 / std, 0.0)
    R = cov * inv_std[:, None] * inv_std[None, :]
    # 平均相关系数 (exclude diagonal)
    off_diag = R[~np.eye(n, dtype=bool)]
    avg_corr = off_diag.mean() if len(off_diag) > 0 else 0.0

    # 目标矩阵: 对角线=avg_var, 非对角线=avg_var × avg_corr
    target = np.full((n, n), avg_var * avg_corr)
    np.fill_diagonal(target, avg_var)
    return target


def ledoit_wolf_cov(
    returns: pd.DataFrame,
    shrinkage: Optional[float] = None,
) -> pd.DataFrame:
    """Ledoit-Wolf 收缩协方差估计。

    returns: DataFrame, index=date, columns=symbols (日收益率)
    shrinkage: 收缩强度 δ ∈ [0,1]。None 时自动估计最优 δ。

    返回: 收缩后的协方差矩阵 DataFrame。

    实现: 常数相关模型（最鲁棒的 LW 变体）。

    来源: ② Ledoit & Wolf (2004) 公式 (14)-(17)
    """
    symbols = returns.columns.tolist()
    n = len(symbols)
    T = len(returns)

    if n < 2 or T < n:
        # 样本不足时用对角协方差
        var = returns.var(ddof=1)
        return pd.DataFrame(np.diag(var.values), index=symbols, columns=symbols)

    # 中心化
    X = returns.values - returns.values.mean(axis=0)  # (T, n)
    S = (X.T @ X) / (T - 1)  # 样本协方差

    target = _constant_correlation_target(S)

    if shrinkage is None:
        # LM 自动估计最优 δ → 公式 Ledoit-Wolf (2004) eq. (17)
        # π̂ = sum over all i,j of AsyVar(s_ij)
        # For constant-correlation target:
        #   δ* = π / γ
        # where π = sum_i sum_j AsyVar(√T * s_ij)
        #       γ = sum_i sum_j (f_ij - s_ij)²

        # π: 渐近方差 (使用无偏一致估计)
        # AsyVar(s_ij) ≈ 1/T * sum_t[(x_ti - x̄_i)(x_tj - x̄_j) - s_ij]²
        pi_mat = np.zeros((n, n))
        for t in range(T):
            diff = np.outer(X[t], X[t]) - S
            pi_mat += diff ** 2
        pi_mat *= T / ((T - 1) ** 3)  # 渐近方差修正
        pi_hat = pi_mat.sum()

        # γ: 样本协方差与目标的距离
        gamma_hat = ((S - target) ** 2).sum()

        # δ* = π̂ / γ̂, clamped to [0, 1]
        shrinkage = max(0.0, min(1.0, pi_hat / max(gamma_hat, 1e-10)))

    # 收缩估计
    shrunk = (1 - shrinkage) * S + shrinkage * target

    return pd.DataFrame(shrunk, index=symbols, columns=symbols)


def covariance_matrix(
    returns: pd.DataFrame,
    method: str = "ledoit_wolf",
    window: int = 60,
    min_periods: int = 30,
) -> pd.DataFrame:
    """统一的协方差估计入口。

    returns: index=date, columns=symbols 的日收益率 DataFrame
    method: sample | ledoit_wolf
    window: 滚动窗口长度（取最近 window 个交易日）
    min_periods: 最少需要的交易天数

    返回: 协方差矩阵 DataFrame
    """
    # 取最近 window 个有效交易日
    recent = returns.iloc[-window:].dropna(axis=1, how="all")

    if len(recent) < min_periods:
        from utils.logger import get_logger
        get_logger("risk.covariance").warning(
            f"covariance: only {len(recent)}/{min_periods} periods available"
        )
        # 回退到全部可用数据
        recent = returns.dropna(axis=1, how="all")

    # 只保留有足够数据的股票
    recent = recent.dropna(axis=1, thresh=min_periods)

    if method == "ledoit_wolf":
        return ledoit_wolf_cov(recent)
    return sample_cov(recent)
