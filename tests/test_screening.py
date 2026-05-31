"""测试 factor/screening.py — IC 统计量累加和最终计算"""
import numpy as np
import pandas as pd
import pytest
from factor.screening import _compute_ic_stats, _finalize_ic, _merge_stats


class TestIcStats:
    def test_basic(self, sample_factors, sample_returns):
        common = sample_factors.index.intersection(sample_returns.index)
        X = sample_factors.loc[common]
        y = sample_returns.loc[common]
        stats = _compute_ic_stats(X, y)
        assert len(stats) == len(sample_factors.columns)
        for col, s in stats.items():
            assert "sx" in s and "sy" in s and "sxy" in s and "sx2" in s and "cnt" in s

    def test_perfect_correlation(self):
        """因子与y完全正相关 → IC=1.0"""
        index = pd.MultiIndex.from_product(
            [pd.date_range("2024-01-01", periods=30, freq="B"), ["000001", "000002"]],
            names=["date", "stock"]
        )
        vals = np.linspace(-2, 2, 60)
        X = pd.DataFrame({"perfect": vals}, index=index)
        y = pd.Series(vals, index=index)

        stats = _compute_ic_stats(X, y)
        report = _finalize_ic(stats)
        assert len(report) == 1
        assert abs(report[0]["mean_IC"] - 1.0) < 0.01, f"IC={report[0]['mean_IC']}"

    def test_no_correlation(self):
        """随机因子 → IC≈0"""
        np.random.seed(123)
        index = pd.MultiIndex.from_product(
            [pd.date_range("2024-01-01", periods=60, freq="B"), [f"{i:06d}" for i in range(1, 11)]],
            names=["date", "stock"]
        )
        X = pd.DataFrame({"random": np.random.randn(600)}, index=index)
        y = pd.Series(np.random.randn(600), index=index)

        stats = _compute_ic_stats(X, y)
        report = _finalize_ic(stats)
        assert len(report) == 1
        assert abs(report[0]["mean_IC"]) < 0.3


class TestMergeStats:
    def test_merge(self, sample_factors, sample_returns):
        common = sample_factors.index.intersection(sample_returns.index)
        X = sample_factors.loc[common]
        y = sample_returns.loc[common]

        # Split into two chunks
        mid = len(common) // 2
        stats_a = _compute_ic_stats(X.iloc[:mid], y.iloc[:mid])
        stats_b = _compute_ic_stats(X.iloc[mid:], y.iloc[mid:])
        merged = _merge_stats(stats_a, stats_b)

        # Full computation
        full_stats = _compute_ic_stats(X, y)
        full_report = _finalize_ic(full_stats)
        merged_report = _finalize_ic(merged)

        for fr, mr in zip(full_report, merged_report):
            # Merged IC should be very close to full IC
            assert abs(fr["mean_IC"] - mr["mean_IC"]) < 0.1
