"""config loader.override + hyperopt 参数注入 (待办 #9 / test-v298).

旧版 hyperopt 把参数写进 OPTUNA_* 环境变量, 但全项目无人读 — Optuna
在优化常数目标函数。修复后: config 参数经 loader.override() 覆盖单例,
universe_size / combine_mode 走 run_backtest 显式参数。
"""
import pytest

from quant.config import loader
from quant.config.constants import _require_cfg


class TestConfigOverride:
    """loader.override 上下文管理器."""

    def test_override_visible_and_restored(self):
        key = "optimizer.tc_lambda"
        original = _require_cfg(key)
        with loader.override({key: 2.5}):
            assert _require_cfg(key) == 2.5
        assert _require_cfg(key) == original

    def test_override_nested_key(self):
        key = "risk.covariance.window"
        original = _require_cfg(key)
        with loader.override({key: 99}):
            assert _require_cfg(key) == 99
        assert _require_cfg(key) == original

    def test_unknown_key_raises(self):
        """禁止通过 override 新增配置项 (fail-fast 防笔误)."""
        with pytest.raises(KeyError, match="不存在"):
            with loader.override({"nonexistent.deep.key": 1}):
                pass

    def test_restore_after_exception(self):
        key = "optimizer.tc_lambda"
        original = _require_cfg(key)
        with pytest.raises(RuntimeError, match="boom"):
            with loader.override({key: 9.9}):
                raise RuntimeError("boom")
        assert _require_cfg(key) == original


class TestHyperoptInjection:
    """hyperopt 参数注入接线 (旧 OPTUNA_* env 机制的替代)."""

    # fake trial 的固定采样值
    _VALUES = {
        "n_symbols": 300,
        "lookback_days": 500,
        "top_fraction": 0.25,
        "max_positions": 15,
        "covariance_window": 60,
        "atr_sl": 2.0,
        "atr_tp1": 2.0,
        "atr_tp2": 3.0,
        "combine_mode": "sleeve",
        "rebalance_freq": "daily",
    }

    class _FakeTrial:
        """最小 Optuna trial 替身: suggest_* 返回固定值."""

        def __init__(self, values):
            self._v = values
            self.attrs = {}
            self.number = 7

        def suggest_int(self, name, low, high, step=1):
            assert low <= self._v[name] <= high
            return self._v[name]

        def suggest_float(self, name, low, high, step=None):
            assert low <= self._v[name] <= high
            return self._v[name]

        def suggest_categorical(self, name, choices):
            assert self._v[name] in choices
            return self._v[name]

        def set_user_attr(self, key, value):
            self.attrs[key] = value

    def test_param_map_keys_exist_in_config(self):
        """_PARAM_TO_CONFIG 的每个 config key 都必须真实存在."""
        from quant.optimizer.hyperopt import _PARAM_TO_CONFIG
        for cfg_key in _PARAM_TO_CONFIG.values():
            assert _require_cfg(cfg_key) is not None, f"missing config key: {cfg_key}"

    def test_objective_applies_overrides_and_kwargs(self, monkeypatch):
        """objective() 运行期间: config 单例被覆盖 + 显式参数传入 run_backtest."""
        import quant.optimizer.hyperopt as hyperopt
        from quant.config.constants import _require_cfg as req

        captured = {}

        def fake_run_backtest(**kwargs):
            for cfg_key in hyperopt._PARAM_TO_CONFIG.values():
                captured[cfg_key] = req(cfg_key)
            captured["kwargs"] = kwargs
            return {
                "metrics": {"sharpe": 1.23, "max_drawdown_pct": -10.0,
                            "cagr_pct": 20.0, "final_equity": 6000.0},
                "errors": 0,
            }

        monkeypatch.setattr("quant.backtest.loop.run_backtest", fake_run_backtest)

        v = self._VALUES
        before = {k: req(k) for k in hyperopt._PARAM_TO_CONFIG.values()}
        sharpe = hyperopt.objective(self._FakeTrial(v))

        # 目标函数返回 trial 的 sharpe
        assert sharpe == pytest.approx(1.23)
        # config 覆盖在回测内生效
        assert captured["data.lookback_days"] == v["lookback_days"]
        assert captured["alpha.top_fraction"] == pytest.approx(v["top_fraction"])
        assert captured["risk.max_positions"] == v["max_positions"]
        assert captured["risk.covariance.window"] == v["covariance_window"]
        assert captured["risk.atr_mult_stop_loss"] == pytest.approx(v["atr_sl"])
        assert captured["risk.atr_mult_take_profit_1"] == pytest.approx(v["atr_tp1"])
        assert captured["risk.atr_mult_take_profit_2"] == pytest.approx(v["atr_tp2"])
        assert captured["optimizer.rebalance_freq"] == v["rebalance_freq"]
        # 显式参数
        assert captured["kwargs"]["universe_size"] == v["n_symbols"]
        assert captured["kwargs"]["combine_mode"] == v["combine_mode"]
        assert captured["kwargs"]["strategy"] == "optuna_7"
        # 退出后 config 恢复
        for k, orig in before.items():
            assert req(k) == orig

    def test_objective_returns_zero_on_backtest_error(self, monkeypatch):
        import quant.optimizer.hyperopt as hyperopt

        monkeypatch.setattr("quant.backtest.loop.run_backtest",
                            lambda **kw: {"error": "no data"})
        sharpe = hyperopt.objective(self._FakeTrial(self._VALUES))
        assert sharpe == 0.0

    def test_objective_drawdown_penalty(self, monkeypatch):
        """MDD > 30% → sharpe × 0.5."""
        import quant.optimizer.hyperopt as hyperopt

        monkeypatch.setattr(
            "quant.backtest.loop.run_backtest",
            lambda **kw: {"metrics": {"sharpe": 2.0, "max_drawdown_pct": -35.0,
                                      "cagr_pct": 10.0, "final_equity": 4000.0},
                          "errors": 0})
        sharpe = hyperopt.objective(self._FakeTrial(self._VALUES))
        assert sharpe == pytest.approx(1.0)
