"""测试 strategy/signals.py — 信号生成和权重"""
import numpy as np
import pandas as pd
import pytest
from strategy.signals import generate_signals, generate_weights


class TestGenerateSignals:
    def test_basic(self):
        pred = pd.Series([0.5, 0.3, 0.2, 0.1, 0.05],
                         index=["a", "b", "c", "d", "e"])
        sig = generate_signals(pred, top_pct=0.4)
        # top 40% of 5 = 2 stocks
        assert sig.sum() == 2
        assert sig["a"] == 1
        assert sig["b"] == 1

    def test_min_one(self):
        """至少选1只，即使top_pct很小"""
        pred = pd.Series([0.5, 0.3], index=["a", "b"])
        sig = generate_signals(pred, top_pct=0.01)
        assert sig.sum() == 1

    def test_all_selected(self):
        pred = pd.Series([0.5, 0.3], index=["a", "b"])
        sig = generate_signals(pred, top_pct=1.0)
        assert sig.sum() == 2


class TestGenerateWeights:
    def test_equal(self):
        sig = pd.Series([1, 1, 1, 0, 0], index=list("abcde"))
        w = generate_weights(sig, method="equal")
        assert abs(w.sum() - 1.0) < 1e-10
        assert w["a"] == 1/3
        assert w["d"] == 0

    def test_prediction_weighted(self):
        sig = pd.Series([1, 1, 0, 0], index=list("abcd"))
        pred = pd.Series([0.6, 0.2, 0.1, 0.1], index=list("abcd"))
        w = generate_weights(sig, pred, method="prediction")
        assert abs(w.sum() - 1.0) < 1e-10
        assert w["a"] > w["b"]  # higher prediction should get higher weight

    def test_all_negative(self):
        """C7: 全部预测为负时回退等权"""
        sig = pd.Series([1, 1, 1], index=list("abc"))
        pred = pd.Series([-1.0, -2.0, -3.0], index=list("abc"))
        w = generate_weights(sig, pred, method="prediction")
        assert not w.isna().any(), "should not produce NaN"
        assert abs(w.sum() - 1.0) < 1e-10
        assert (w[w > 0] == 1.0 / 3).all(), "should fall back to equal weight"

    def test_no_signals(self):
        sig = pd.Series([0, 0, 0], index=list("abc"))
        w = generate_weights(sig, method="equal")
        assert w.sum() == 0
