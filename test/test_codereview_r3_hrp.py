"""v418 (R3): HRP linkage-tree 切分回归测试.

背景 (CODE-REVIEW-2026-08-07 Debt 5): 原 _recursive_bisection 按 `len//2`
中点切分, 不遵循层次聚类树 — 高相关股票可能被分到不同子簇, 风险预算次优.
修复后按 linkage 树节点切分 (quasi-diagonal 排序下左右子树为连续段).

验证:
1. 高相关 3 资产 + 1 独立资产: 公开独立资产应获显著更高权重 (危险分散贡献),
   且高相关组内权重总占 < 独立资产 (系统性行为受控).
2. 对称协方差 → 权重均匀 (退化路径健壮).
3. link=None 兼容旧中点路径 (不抛错).
"""
import numpy as np
import pytest

from quant.optimizer.hrp import hrp_weights


def _make_cov(n: int, seed: int = 0, corr_group: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.standard_normal((300, 1)) * 0.05
    cols = []
    for i in range(n):
        if corr_group and i < corr_group:
            cols.append(base + 0.001 * rng.standard_normal((300, 1)))
        else:
            cols.append(rng.standard_normal((300, 1)) * 0.05)
    return np.cov(np.hstack(cols).T)


def test_tree_split_gives_independent_asset_higher_weight():
    # 3 只高相关 + 1 只独立 → 独立资产 (D) 应显著 > 组内单只
    cov = _make_cov(4, seed=3, corr_group=3)
    w = hrp_weights(cov)
    assert abs(w.sum() - 1.0) < 1e-9
    assert (w > 0).all()
    w_group = w[:3].sum()
    w_alone = w[3]
    assert w_alone > w_group, \
        f"独立资产权重 {w_alone:.3f} 应高于高相关组总量 {w_group:.3f} (树切分后风险平价为"


def test_equal_weight_degenerate():
    cov = np.eye(4)
    w = hrp_weights(cov)
    assert np.allclose(w, 0.25, atol=1e-6)


def test_two_asset_fallback():
    w = hrp_weights(np.array([[1.0, 1.0], [1.0, 1.0]]))
    assert np.allclose(w, 0.5, atol=1e-9)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))