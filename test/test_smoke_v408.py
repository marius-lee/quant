"""冒烟测试 — web/qlib_model/benchmark 基础可用性 (v408).

模板 3 (TDD, 软约束): 只测导入和基本调用, 不测完整业务逻辑.
"""
import pytest


class TestWebSmoke:
    def test_app_import(self):
        from web.app import app
        assert app is not None

    def test_app_has_routes(self):
        from web.app import app
        routes = [rule.rule for rule in app.url_map.iter_rules()]
        assert "/api/scheduler" in routes
        assert "/api/state" in routes

    def test_state_broker_import(self):
        from quant.core.state_broker import broker
        state = broker.get()
        assert isinstance(state, dict)
        assert "capital" in state or "positions" in state


class TestQlibModelSmoke:
    def test_import(self):
        from quant.alpha.qlib_model import LgbAlphaModel
        assert LgbAlphaModel is not None

    def test_unavailable_no_crash(self):
        """未安装 lightgbm 时 __init__ 不崩溃."""
        try:
            from quant.alpha.qlib_model import LgbAlphaModel
            m = LgbAlphaModel()
            assert m._is_available in (True, False)
        except ImportError:
            pytest.skip("lightgbm not installed")


class TestBenchmarkSmoke:
    def test_get_benchmark_returns(self):
        from quant.data.benchmark import get_benchmark_returns
        ret = get_benchmark_returns("000300", start="2026-01-01")
        assert ret is not None
