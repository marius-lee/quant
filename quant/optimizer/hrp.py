"""Hierarchical Risk Parity (HRP) — De Prado (2016).

ALG3: HRP 作为 Ledoit-Wolf + mean-variance 的替代优化器。
不直接求逆协方差矩阵，基于层次聚类分配风险预算，对高维截面更稳定。

用法:
  from quant.optimizer.hrp import hrp_weights
  weights = hrp_weights(cov_matrix)
"""

import numpy as np
from scipy.cluster.hierarchy import linkage, fcluster
from scipy.spatial.distance import squareform

from quant.utils.logger import get_logger

_log = get_logger("quant.optimizer.hrp")


def _correlation_from_cov(cov: np.ndarray) -> np.ndarray:
    """协方差矩阵 → 相关系数矩阵。"""
    std = np.sqrt(np.diag(cov))
    std[std < 1e-10] = 1e-10
    return cov / np.outer(std, std)


def _quasi_diagonalize(link: np.ndarray, n: int) -> list:
    """从 linkage 矩阵生成 quasi-diagonal 排序（De Prado 2016, pp. 66-68）。"""
    # 叶子节点排序
    sorted_idx = []
    def _walk(node):
        if node < n:
            sorted_idx.append(node)
            return
        left = int(link[node - n, 0])
        right = int(link[node - n, 1])
        _walk(left)
        _walk(right)
    _walk(2 * n - 2)  # root node
    return sorted_idx


def _recursive_bisection(cov: np.ndarray, sorted_idx: list) -> np.ndarray:
    """递归二分分配风险预算 — De Prado (2016), pp. 68-72。"""
    w = np.ones(len(sorted_idx), dtype=float)
    cluster_items = [sorted_idx]

    while len(cluster_items) > 0:
        # 所有簇已不可再分 → 终止
        if all(len(items) <= 1 for items in cluster_items):
            break
        # 二分每个簇
        bisected = []
        for items in cluster_items:
            if len(items) <= 1:
                bisected.append(items)
                continue

            # 按方差加权 split
            n_left = len(items) // 2
            left = items[:n_left]
            right = items[n_left:]

            # 子簇方差 (逆方差加权)
            cov_left = cov[np.ix_(left, left)]
            cov_right = cov[np.ix_(right, right)]
            var_left = 1.0 / max(np.diag(cov_left).sum() if cov_left.size > 1 else cov_left[0, 0], 1e-8)
            var_right = 1.0 / max(np.diag(cov_right).sum() if cov_right.size > 1 else cov_right[0, 0], 1e-8)

            alpha = var_left / (var_left + var_right)

            # 分配权重: 按风险平价分配子簇间，子簇内等权
            w_left = alpha / len(left)
            w_right = (1 - alpha) / len(right)
            for idx in left:
                w[idx] = w_left
            for idx in right:
                w[idx] = w_right

            bisected.append(left)
            bisected.append(right)

        cluster_items = bisected

    # 归一化
    return w / w.sum()


def hrp_weights(cov: np.ndarray, linkage_method: str = "ward") -> np.ndarray:
    """Hierarchical Risk Parity 权重。

    Args:
        cov: 协方差矩阵 (N×N)
        linkage_method: 层次聚类方法 (ward/single/complete/average)，默认 ward

    Returns:
        weights: 归一化权重 (N,)

    Source: De Prado, M. (2016) "Building Diversified Portfolios that
            Outperform Out-of-Sample", Journal of Portfolio Management.
    """
    n = cov.shape[0]

    if n <= 2:
        # 退化: 等权
        _log.debug("HRP: n=%d ≤ 2, returning equal weight", n)
        return np.ones(n) / n

    # 1. 相关系数 → 距离矩阵
    corr = _correlation_from_cov(cov)
    # 距离: d = sqrt(0.5 * (1 - ρ)) — De Prado (2016) Eq. 2
    np.fill_diagonal(corr, 1.0)
    dist = np.sqrt(0.5 * (1.0 - corr))
    dist = np.clip(dist, 0, None)

    # 2. 层次聚类
    try:
        condensed = squareform(dist, checks=False)
        link = linkage(condensed, method=linkage_method)
    except Exception:
        _log.warning("HRP: linkage failed, returning equal weight")
        return np.ones(n) / n

    # 3. Quasi-diagonal 排序
    sorted_idx = _quasi_diagonalize(link, n)

    # 4. 递归二分分配权重
    weights = _recursive_bisection(cov, sorted_idx)

    _log.debug("HRP: %d assets, linkage=%s, weights range [%.4f, %.4f]",
               n, linkage_method, weights.min(), weights.max())
    return weights
