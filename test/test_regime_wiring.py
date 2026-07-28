"""§8.2 (test-v299): Regime 条件合成接线 — pipeline/回测 point-in-time.

覆盖:
  - RegimeDetector 训练 + 前向滤波 predict_proba (回测 PIT 用法)
  - AlphaModel.combine_regime 权重偏置生效
  - generate_signals 接受 regime 注入参数 (pipeline 接线签名)
"""
import inspect

import numpy as np
import pandas as pd
import pytest

from quant.alpha.model import AlphaModel
from quant.regime.detector import REGIME_LABELS, RegimeDetector, get_regime_weights


def _synthetic_returns(n=400, seed=7):
    """3 段合成收益 (百分口径): 牛(正漂移低波) → 熊(负漂移高波) → 震荡(零漂移)."""
    rng = np.random.default_rng(seed)
    bull = rng.normal(0.15, 0.6, n // 3)
    bear = rng.normal(-0.20, 1.8, n // 3)
    side = rng.normal(0.0, 0.4, n - 2 * (n // 3))
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.Series(np.concatenate([bull, bear, side]), index=idx)


class TestRegimeDetectorPIT:
    """回测 point-in-time 用法: 起始日前训练, 截止当日前向滤波."""

    def test_train_and_predict_returns_valid_label(self):
        rets = _synthetic_returns()
        det = RegimeDetector().train(rets)
        assert det._model is not None
        label, probs = det.predict_proba(rets)
        assert label in REGIME_LABELS.values()
        assert set(probs.keys()) == set(REGIME_LABELS.values())
        assert sum(probs.values()) == pytest.approx(1.0, abs=1e-6)

    def test_predict_on_truncated_series_no_lookahead(self):
        """截断序列 predict 不崩 — 回测逐日注入的正是 trailing 视图."""
        rets = _synthetic_returns()
        train_end = rets.index[199]
        det = RegimeDetector().train(rets[rets.index <= train_end])
        for cut in (250, 300, 399):
            label, probs = det.predict_proba(rets.iloc[:cut])
            assert label in set(REGIME_LABELS.values()) | {"unknown"}

    def test_untrained_detector_returns_unknown(self):
        det = RegimeDetector()
        label, probs = det.predict_proba(_synthetic_returns())
        assert label == "unknown"
        assert probs == {}

    def test_insufficient_train_samples_no_model(self):
        det = RegimeDetector().train(_synthetic_returns(n=60))
        assert det._model is None


class TestCombineRegime:
    def test_bull_boosts_momentum_keywords(self):
        """牛市 + 高置信 → 含 'momentum' 的因子权重提升, 其余不变."""
        ic_map = {"momentum_20d": 1.0, "volatility_20d": 1.0}
        w = get_regime_weights(list(ic_map), ic_map, "bull", {"bull": 0.9})
        assert w["momentum_20d"] > w["volatility_20d"]

    def test_unknown_label_returns_ic_map_unchanged(self):
        ic_map = {"f1": 0.5, "f2": 0.5}
        assert get_regime_weights(list(ic_map), ic_map, "unknown", {}) == ic_map

    def test_combine_regime_changes_alpha(self):
        """composite 模式: regime 偏置改变合成得分."""
        syms = [f"S{i}" for i in range(20)]
        rng = np.random.default_rng(1)
        fv = {
            "momentum_20d": pd.Series(rng.normal(0, 1, 20), index=syms),
            "volatility_20d": pd.Series(rng.normal(0, 1, 20), index=syms),
        }
        am = AlphaModel(combine_mode="composite", method="ic_weighted")
        ic_map = {"momentum_20d": 1.0, "volatility_20d": 1.0}
        plain = am.combine(fv, ic_map=ic_map)
        regime = am.combine_regime(fv, ic_map=ic_map, regime_label="bull",
                                   regime_probs={"bull": 0.9})
        assert not np.allclose(plain.values, regime.values)

    def test_combine_regime_unknown_falls_back(self):
        syms = [f"S{i}" for i in range(10)]
        fv = {"f1": pd.Series(np.arange(10.0), index=syms)}
        am = AlphaModel(combine_mode="composite", method="equal_weight")
        plain = am.combine(fv, ic_map=None)
        regime = am.combine_regime(fv, ic_map=None, regime_label="unknown")
        pd.testing.assert_series_equal(plain, regime)


class TestPipelineSignature:
    def test_generate_signals_accepts_regime_params(self):
        from quant.pipeline import generate_signals
        params = inspect.signature(generate_signals).parameters
        assert "regime_label" in params
        assert "regime_probs" in params

    def test_run_backtest_uses_pit_regime(self):
        """loop.py 含 point-in-time regime 注入 (防前视回归守卫)."""
        src = inspect.getsource(__import__("quant.backtest.loop", fromlist=["run_backtest"]))
        assert "RegimeDetector" in src
        assert "regime_label" in src
        assert "get_current_regime(" not in src  # 回测禁止全量训练模型
