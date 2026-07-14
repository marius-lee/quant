"""P43: 分仓合成 sleeve_compose 测试."""
import pytest
import pandas as pd
import numpy as np
import sys
sys.path.insert(0, '.')

from quant.factor.synth import sleeve_compose, ic_weighted, equal_weight


class TestSleeveCompose:
    """sleeve_compose: 每因子独立选 top N, 取并集."""

    def test_basic_sleeve(self):
        fv = {
            'f1': pd.Series([0.9, 0.5, 0.1, -0.2], index=['A', 'B', 'C', 'D']),
            'f2': pd.Series([-0.1, 0.8, 0.3, 0.7], index=['A', 'B', 'C', 'D']),
        }
        result = sleeve_compose(fv, positions_per_factor=2, min_factors=1)
        # f1 top 2: A(0.9), B(0.5)
        # f2 top 2: B(0.8), D(0.7)
        # union: A, B, D
        assert set(result.index) == {'A', 'B', 'D'}
        # P43: sleeve_compose now returns raw z-score, not equal-weight 1.0
        assert result["A"] == 0.9
        assert result["B"] == 0.8  # max(f1=0.5, f2=0.8) = 0.8
        assert result["D"] == 0.7

    def test_overlap_sleeve(self):
        fv = {
            'f1': pd.Series([0.9, 0.8], index=['A', 'B']),
            'f2': pd.Series([0.7, 0.6], index=['A', 'B']),
        }
        result = sleeve_compose(fv, positions_per_factor=2, min_factors=1)
        # Both pick A and B — union = {A, B}
        assert set(result.index) == {'A', 'B'}
        assert len(result) == 2

    def test_empty_factor_values(self):
        result = sleeve_compose({}, positions_per_factor=1, min_factors=0)
        assert len(result) == 0

    def test_too_few_factors(self):
        fv = {'f1': pd.Series([0.9], index=['A'])}
        result = sleeve_compose(fv, positions_per_factor=1, min_factors=2)
        assert len(result) == 0

    def test_nan_handling(self):
        fv = {
            'f1': pd.Series([0.9, np.nan, 0.5], index=['A', 'B', 'C']),
            'f2': pd.Series([0.8, 0.3, np.nan], index=['A', 'B', 'C']),
        }
        result = sleeve_compose(fv, positions_per_factor=1, min_factors=1)
        # f1 top 1: A (0.9), skip NaN B
        # f2 top 1: A (0.8), skip NaN C
        # union: A
        assert list(result.index) == ['A']

    def test_sort_descending(self):
        fv = {
            'f1': pd.Series([-2.0, -1.0, 3.0, 0.5], index=['W', 'X', 'Y', 'Z']),
        }
        result = sleeve_compose(fv, positions_per_factor=2, min_factors=1)
        # f1 top 2 by value: Y(3.0), Z(0.5)
        assert set(result.index) == {'Y', 'Z'}
