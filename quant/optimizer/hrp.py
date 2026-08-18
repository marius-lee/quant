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


def _recursive_bisection(cov: np.ndarray, sorted_idx: list, link: np.ndarray = None) -> np.ndarray:
    """递归二分分配风险预算 — De Prado (2016), pp. 68-72。

    v418 (R3): 原实现按 `len//2` 中点切分, 不尊重层次聚类树结构 —
    相关性高的两只股票可能被硬生生分到不同子簇, 导致风险平价次优。
    修复: 按 linkage 树自顶向下切分, 每个内部节点沿其左/右子树自然分裂
    (quasi-diagonal 排序保证左子树叶子连续排在右子树之前)。

    Args:
        cov: 协方差矩阵 (N×N)
        sorted_idx: quasi-diagonal 排序后的索引列表
        link: 层次聚类 linkage 矩阵; None 时回退中点切分 (兼容旧调用)
    """
    n = len(sorted_idx)
    if n <= 1:
        return np.ones(n, dtype=float)

    w = np.zeros(n, dtype=float)
    left_leaf: dict[int, int] = {}

    def _count_leaves(node: int) -> int:
        """递归统计子树叶子数, 缓存到 left_leaf[i] (i = node - n)."""
        if node < n:
            return 1
        i = node - n
        if i in left_leaf:
            return left_leaf[i]
        nl = _count_leaves(int(link[i, 0]))
        _count_leaves(int(link[i, 1]))
        left_leaf[i] = nl
        return nl

    def _cluster_var(cov_block) -> float:
        """簇方差 — B17 (2026-08-18): 原 diag(cov).sum() 等权口径
        (忽略协方差与权重), 高波动簇方差失真 → alpha 分配偏斜.
        用 IVP (逆方差组合, López de Prado 2016) 权重: var = w' Σ w,
        w ∝ 1/diag(Σ), 纯对角输入时退化为调和均值口径."""
        if np.size(cov_block) == 1:
            return float(np.asarray(cov_block).flat[0])
        diag = np.diag(cov_block)
        inv = 1.0 / np.maximum(diag, 1e-8)
        w = inv / inv.sum()
        return float(w @ cov_block @ w)

    def _bisect(node: int, items: list, factor: float, use_tree: bool):
        """递归切分 items (sorted_idx 的连续段), node 为对应 linkage 节点."""
        if len(items) <= 1:
            w[items[0]] = factor
            return
        if use_tree and node >= n:
            i = node - n
            mid = min(left_leaf.get(i, len(items) // 2), len(items) - 1)
        else:
            mid = len(items) // 2
        if mid <= 0:
            mid = 1
        left_items = items[:mid]
        right_items = items[mid:]

        cov_left = cov[np.ix_(left_items, left_items)]
        cov_right = cov[np.ix_(right_items, right_items)]
        var_left = 1.0 / max(_cluster_var(cov_left), 1e-8)
        var_right = 1.0 / max(_cluster_var(cov_right), 1e-8)
        alpha = var_left / (var_left + var_right)

        if use_tree and node >= n:
            i = node - n
            _bisect(int(link[i, 0]), left_items, factor * alpha, True)
            _bisect(int(link[i, 1]), right_items, factor * (1 - alpha), True)
        else:
            _bisect(-1, left_items, factor * alpha, False)
            _bisect(-1, right_items, factor * (1 - alpha), False)

    use_tree = link is not None and len(link) >= n - 1
    if use_tree:
        _count_leaves(2 * n - 2)
    _bisect(2 * n - 2 if use_tree else -1, list(sorted_idx), 1.0, use_tree)

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

    # v418: 零相关退化守护 — 相关矩阵 off-diagonal 全 0 时, 树无信息 (scipy
    # 对平射距离产生病态不平衡树 → 权重失衡). 最优解 = 逆方差加权 (IVP).
    _off = corr[~np.eye(n, dtype=bool)]
    if _off.size and np.allclose(_off, 0.0):
        _log.debug("HRP: zero correlation, inverse-variance weights")
        ivp = 1.0 / np.maximum(np.diag(cov), 1e-8)
        return ivp / ivp.sum()

    # 3. Quasi-diagonal 排序
    sorted_idx = _quasi_diagonalize(link, n)

    # 4. 递归二分分配权重 (v418: 传 link 走树切分)
    weights = _recursive_bisection(cov, sorted_idx, link)

    _log.debug("HRP: %d assets, linkage=%s, weights range [%.4f, %.4f]",
               n, linkage_method, weights.min(), weights.max())
    return weights
