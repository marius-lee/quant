"""模板 3 (TDD): 边际贡献评估的确定性测试."""
import pytest
import numpy as np
import sys
sys.path.insert(0, '.')

from quant.factor.marginal import compute_marginal_evaluation, rank_candidates


def _make_test_data():
    """构造已知的 IC + 相关性矩阵."""
    # 3 factors: A (high IC), B (correlated with A), C (independent)
    names = ["factor_A", "factor_B", "factor_C"]
    ic_means = {"factor_A": 0.06, "factor_B": 0.05, "factor_C": 0.03}
    ic_irs = {"factor_A": 0.6, "factor_B": 0.5, "factor_C": 0.3}
    # A-B highly correlated, C independent
    corr = np.array([
        [1.0, 0.9, 0.1],
        [0.9, 1.0, 0.0],
        [0.1, 0.0, 1.0],
    ])
    n_days = 90
    return names, ic_means, ic_irs, corr, n_days


class TestMarginalEvaluation:
    """compute_marginal_evaluation: 统计显著性 + 边际贡献."""

    def test_returns_all_factor_results(self):
        names, ic_means, ic_irs, corr, n_days = _make_test_data()
        results = compute_marginal_evaluation(names, ic_means, ic_irs, corr, n_days, t_threshold=2.0)
        assert len(results) == 3
        for name in names:
            assert name in results
            assert "ic" in results[name]
            assert "t_stat" in results[name]

    def test_factor_a_passes_significance(self):
        """IC=0.06, IR=0.6, t = 0.6*√90 ≈ 5.7 > 2.0."""
        names, ic_means, ic_irs, corr, n_days = _make_test_data()
        results = compute_marginal_evaluation(names, ic_means, ic_irs, corr, n_days, t_threshold=2.0)
        assert results["factor_A"]["t_pass"]

    def test_correlated_factor_has_low_marginal(self):
        """B 与 A 高度相关, 边际 IC 应远小于 IC."""
        names, ic_means, ic_irs, corr, n_days = _make_test_data()
        results = compute_marginal_evaluation(names, ic_means, ic_irs, corr, n_days, t_threshold=2.0)
        # B's marginal IC should be much smaller than its raw IC
        marginal_b = results["factor_B"]["marginal_ic"]
        assert marginal_b is not None
        assert abs(marginal_b) < abs(ic_means["factor_B"] * 0.5)

    def test_independent_factor_retains_most_ic(self):
        """C 与其他因子不相关, 边际 IC 应接近原始 IC."""
        names, ic_means, ic_irs, corr, n_days = _make_test_data()
        results = compute_marginal_evaluation(names, ic_means, ic_irs, corr, n_days, t_threshold=2.0)
        marginal_c = results["factor_C"]["marginal_ic"]
        assert marginal_c is not None
        assert abs(marginal_c) > 0.02  # Should retain significant IC

    def test_rank_candidates_returns_sorted(self):
        names, ic_means, ic_irs, corr, n_days = _make_test_data()
        results = compute_marginal_evaluation(names, ic_means, ic_irs, corr, n_days, t_threshold=2.0)
        ranked = rank_candidates(results)
        assert len(ranked) == 3
        # Should be sorted by |marginal_ic| descending
        mics = [abs(r[1]) for r in ranked]
        assert all(mics[i] >= mics[i+1] for i in range(len(mics)-1))

    def test_no_factors_returns_empty(self):
        results = compute_marginal_evaluation([], {}, {}, np.array([]), 90)
        assert results == {}


class TestEdgeCases:
    """边际贡献的异常输入."""

    def test_singular_correlation_matrix(self):
        """两个完全相同因子的边际贡献."""
        names = ["f1", "f2"]
        ic_means = {"f1": 0.05, "f2": 0.05}
        ic_irs = {"f1": 0.5, "f2": 0.5}
        corr = np.array([[1.0, 1.0], [1.0, 1.0]])  # perfect correlation → singular
        results = compute_marginal_evaluation(names, ic_means, ic_irs, corr, 90)
        assert "f2" in results
        # Should handle singular matrix gracefully

    def test_missing_ir_fails_significance(self):
        """IR 为 0 的因子应无法通过 t 检验."""
        names = ["f1"]
        ic_means = {"f1": 0.04}
        ic_irs = {"f1": 0.0}
        corr = np.eye(1)
        results = compute_marginal_evaluation(names, ic_means, ic_irs, corr, 90)
        assert not results["f1"]["t_pass"]  # t = 0 * √90 = 0
