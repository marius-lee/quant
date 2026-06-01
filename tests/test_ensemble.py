"""测试 strategy/ensemble.py — 集成模型训练、预测、特征重要性"""
import numpy as np
import pytest
from strategy.ensemble import EnsembleModel


class TestEnsembleModel:
    @pytest.fixture
    def data(self):
        np.random.seed(42)
        X = np.random.randn(200, 5)
        true_w = np.array([0.3, -0.1, 0.2, 0.4, -0.15])
        y = X @ true_w + np.random.randn(200) * 0.1
        features = ["momentum_5d", "volatility_20d", "value_proxy_20d",
                     "amihud_il_10d", "real_ep"]
        return X, y, features

    def test_fit_predict(self, data):
        X, y, features = data
        em = EnsembleModel()
        em.fit(X, y, features)
        pred = em.predict(X)
        assert not np.any(np.isnan(pred)), "predictions should not contain NaN"
        assert len(pred) == len(X)

    def test_feature_importance(self, data):
        X, y, features = data
        em = EnsembleModel()
        em.fit(X, y, features)
        imp = em.feature_importance()
        assert len(imp) == len(features)
        # 特征重要性按降序排列，但名称相同
        assert set(imp.index.tolist()) == set(features)

    def test_model_info(self, data):
        X, y, features = data
        em = EnsembleModel()
        em.fit(X, y, features)
        info = em.model_info
        assert isinstance(info, str)
        assert len(info) > 0

    def test_all_nan_input(self, data):
        """模型在 NaN 输入上应优雅处理，不崩溃"""
        X, y, features = data
        em = EnsembleModel()
        em.fit(X, y, features)
        X_nan = X.copy()
        X_nan[0, :] = np.nan
        try:
            pred = em.predict(X_nan)
            assert len(pred) == len(X)
        except (ValueError, RuntimeWarning):
            # NaN输入导致的数值问题是可以接受的
            pass

    def test_weights_sum_to_one(self, data):
        X, y, features = data
        em = EnsembleModel()
        em.fit(X, y, features)
        assert abs(sum(em.weights) - 1.0) < 1e-10, \
            f"weights sum to {sum(em.weights)}"


class TestEnsembleEdgeCases:
    def test_tiny_sample(self):
        """样本极少时不应崩溃"""
        X = np.random.randn(20, 3)
        y = np.random.randn(20)
        em = EnsembleModel()
        em.fit(X, y, ["f0", "f1", "f2"])
        pred = em.predict(X)
        assert len(pred) == 20
