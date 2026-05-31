"""测试 backtest/metrics.py — 绩效指标计算"""
import numpy as np
import pandas as pd
import pytest
from backtest.metrics import compute_metrics, TRADING_DAYS


class TestComputeMetrics:
    def test_positive_returns(self):
        """持续正收益"""
        r = pd.Series(np.ones(252) * 0.001)  # 每天0.1%
        m = compute_metrics(r)
        assert m["annual_return"] > 0, "should have positive annual return"
        # 固定正收益：std≈0，由于数值精度sharpe会极大（或inf）
        assert m["max_drawdown"] == 0, "no drawdown with constant positive returns"
        assert m["win_rate"] == 1.0, "all days positive"

    def test_negative_returns(self):
        """持续负收益"""
        r = pd.Series(np.ones(252) * -0.001)
        m = compute_metrics(r)
        assert m["annual_return"] < 0
        assert m["win_rate"] == 0.0

    def test_with_benchmark(self):
        np.random.seed(42)
        r = pd.Series(np.random.randn(252) * 0.01 + 0.0005)
        b = pd.Series(np.random.randn(252) * 0.008)
        m = compute_metrics(r, b)
        assert "alpha" in m
        assert "beta" in m
        assert "information_ratio" in m

    def test_empty_returns(self):
        m = compute_metrics(pd.Series([], dtype=float))
        assert m == {}

    def test_short_history(self):
        """短期回测：应有 warning 但不应崩溃"""
        r = pd.Series(np.random.randn(30) * 0.01)
        m = compute_metrics(r)
        assert "annual_return" in m
        assert m["total_days"] == 30

    def test_single_day(self):
        r = pd.Series([0.01])
        m = compute_metrics(r)
        assert "annual_return" in m  # 不应崩溃


class TestMetricsConsistency:
    def test_cumulative_matches_final(self):
        r = pd.Series(np.random.randn(252) * 0.01)
        m = compute_metrics(r, initial_capital=100_000)
        cumulative = (1 + r).cumprod()
        expected_final = 100_000 * cumulative.iloc[-1]
        assert abs(m["final_value"] - expected_final) < 0.01

    def test_max_drawdown_sign(self):
        r = pd.Series(np.random.randn(252) * 0.01)
        m = compute_metrics(r)
        assert m["max_drawdown"] <= 0, "max_drawdown should be zero or negative"
