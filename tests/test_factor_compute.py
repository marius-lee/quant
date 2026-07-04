"""模板 3 (TDD): 因子计算函数的确定性测试. 纯函数 → 输入输出可精确定义."""
import pytest
import pandas as pd
import numpy as np
import sys
sys.path.insert(0, '.')

from factor.compute import (
    _cs_zscore, compute_roe_reported, compute_roa,
    compute_debt_ratio, compute_accruals,
)


class TestCrossSectionalZScore:
    """_cs_zscore: 截面标准化, 所有因子共用."""

    def test_normal_input(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=list("abcde"))
        result = _cs_zscore(s, min_count=3)
        assert result.notna().all()
        assert abs(result.mean()) < 0.001  # z-score mean ≈ 0
        assert abs(result.std() - 1.0) < 0.01  # z-score std ≈ 1

    def test_with_outliers(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 100.0], index=list("abcde"))
        result = _cs_zscore(s, min_count=3)
        assert result.notna().all()
        # outlier should have highest z-score
        assert result.loc["e"] == result.max()

    def test_insufficient_data_returns_nan(self):
        s = pd.Series([1.0, 2.0], index=list("ab"))
        result = _cs_zscore(s, min_count=3)
        assert result.isna().all()

    def test_all_same_values(self):
        s = pd.Series([5.0, 5.0, 5.0, 5.0, 5.0], index=list("abcde"))
        result = _cs_zscore(s, min_count=3)
        assert (result == 0.0).all() or result.isna().any()  # zero or NaN for constant

    def test_nan_handling(self):
        s = pd.Series([1.0, np.nan, 3.0, 4.0, 5.0], index=list("abcde"))
        result = _cs_zscore(s, min_count=3)
        assert pd.isna(result.loc["b"])
        assert result.loc["a":"e"].dropna().notna().all()


class TestFinancialFactors:
    """4 个财务因子: 纯计算, 不依赖 IO."""

    def test_roe_reported_normal(self, sample_fundamentals, sample_financials):
        syms = sample_financials.index.tolist()
        result = compute_roe_reported(
            sample_fundamentals.loc[syms], "2026-06-30", financials=sample_financials
        )
        assert len(result) == len(syms)
        assert result.notna().any()  # at least some valid values

    def test_roe_reported_empty_financials(self, sample_fundamentals):
        empty_fin = pd.DataFrame()
        syms = sample_fundamentals.index[:10].tolist()
        result = compute_roe_reported(
            sample_fundamentals.loc[syms], "2026-06-30", financials=empty_fin
        )
        assert result.isna().all()

    def test_roa_normal(self, sample_fundamentals):
        fin = pd.DataFrame({
            "net_profit": [1e8, 2e8],
            "total_assets": [1e9, 2e9],
        }, index=sample_fundamentals.index[:2].tolist())
        result = compute_roa(
            sample_fundamentals.iloc[:2], "2026-06-30", financials=fin
        )
        assert len(result) == 2

    def test_debt_ratio_normal(self, sample_fundamentals):
        fin = pd.DataFrame({
            "total_liability": [5e8, 3e8],
            "total_assets": [1e9, 2e9],
        }, index=sample_fundamentals.index[:2].tolist())
        result = compute_debt_ratio(
            sample_fundamentals.iloc[:2], "2026-06-30", financials=fin
        )
        assert len(result) == 2

    def test_accruals_normal(self, sample_fundamentals):
        fin = pd.DataFrame({
            "net_profit": [1e8, 2e8],
            "net_operate_cash_flow": [8e7, 3e8],
            "total_assets": [1e9, 2e9],
        }, index=sample_fundamentals.index[:2].tolist())
        result = compute_accruals(
            sample_fundamentals.iloc[:2], "2026-06-30", financials=fin
        )
        assert len(result) == 2

    def test_accruals_missing_column(self, sample_fundamentals):
        """缺少 net_operate_cash_flow 时应返回全 NaN."""
        bad_fin = pd.DataFrame({
            "net_profit": [1e8, 2e8],
            "total_assets": [1e9, 2e9],
        }, index=["000001", "000002"])
        syms = sample_fundamentals.index[:2].tolist()
        result = compute_accruals(
            sample_fundamentals.loc[syms], "2026-06-30", financials=bad_fin
        )
        assert result.isna().all()

    def test_all_factors_same_output_with_same_input(self, sample_fundamentals, sample_financials):
        """同一输入多次调用应返回一致结果."""
        syms = sample_financials.index.tolist()
        r1 = compute_roe_reported(
            sample_fundamentals.loc[syms], "2026-06-30", financials=sample_financials
        )
        r2 = compute_roe_reported(
            sample_fundamentals.loc[syms], "2026-06-30", financials=sample_financials
        )
        pd.testing.assert_series_equal(r1, r2)
