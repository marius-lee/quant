"""测试 factor/base.py — winsorize_mad, normalize_zscore, BaseFactor"""
import numpy as np
import pandas as pd
import pytest
from factor.base import winsorize_mad, normalize_zscore


class TestWinsorizeMad:
    def test_normal(self):
        s = pd.Series([1, 2, 3, 4, 5, 100])
        result = winsorize_mad(s, n=3.0)
        assert result.max() < 100, "outlier not clipped"
        assert result.min() >= 0, "lower clip too aggressive"

    def test_constant(self):
        s = pd.Series([5, 5, 5, 5, 5])
        result = winsorize_mad(s)
        assert (result == 5).all(), "constant series should be unchanged"

    def test_two_elements(self):
        s = pd.Series([1, 100])
        result = winsorize_mad(s)
        assert len(result) == 2
        assert not result.isna().any()


class TestNormalizeZscore:
    def test_normal(self):
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6], "c": [7, 8, 9]})
        result = normalize_zscore(df)
        # Each row should have mean ~0 and std ~1
        assert (result.mean(axis=1).abs() < 1e-10).all()
        assert ((result.std(axis=1) - 1).abs() < 1e-10).all()

    def test_zero_std(self):
        """H1: std=0 should produce 0s, not NaN"""
        df = pd.DataFrame({"a": [1.0, 1.0, 1.0], "b": [1.0, 1.0, 1.0]})
        result = normalize_zscore(df)
        assert not result.isna().any().any(), "std=0 produced NaN!"
        assert (result == 0).all().all(), "std=0 should produce 0 z-scores"

    def test_single_row(self):
        df = pd.DataFrame({"a": [1.0], "b": [2.0]})
        result = normalize_zscore(df)
        assert not result.isna().any().any()


class TestWinsorizeMadEdgeCases:
    def test_all_same(self):
        s = pd.Series([3.0] * 100)
        result = winsorize_mad(s)
        assert (result == 3.0).all()

    def test_with_nan(self):
        s = pd.Series([1.0, 2.0, np.nan, 4.0, 5.0])
        result = winsorize_mad(s)
        assert result.isna().sum() == 1, "NaN should be preserved"
