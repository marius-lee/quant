"""测试 ExecutionEngine — 模拟交易执行 + 资金管理.

模板 3 (TDD): 确定性输入, 状态隔离.
"""
import pytest
from execution.engine import ExecutionEngine, Order


class TestExecutionEngineInit:
    """初始化与资金管理."""

    def test_initial_capital(self):
        engine = ExecutionEngine()
        engine.set_initial_capital("t_ci_1", 10000)
        assert engine.is_initialized("t_ci_1")
        assert engine.get_cash("t_ci_1") == 10000
        assert engine.get_capital("t_ci_1") == 10000

    def test_not_initialized(self):
        engine = ExecutionEngine()
        assert not engine.is_initialized("t_ci_nx")

    def test_multiple_strategies_isolated(self):
        engine = ExecutionEngine()
        engine.set_initial_capital("t_ms_A", 5000)
        engine.set_initial_capital("t_ms_B", 8000)
        assert engine.get_cash("t_ms_A") == 5000
        assert engine.get_cash("t_ms_B") == 8000


class TestExecutionEngineTrade:
    """买入卖出 + 持仓管理."""

    def test_buy_reduces_cash(self):
        engine = ExecutionEngine()
        engine.set_initial_capital("t_tr_b", 50000)
        order = Order(symbol="TICKER1", side="buy", shares=100, price=10.0, cost=0)
        result = engine.execute([order], "2026-07-09", "t_tr_b")
        assert result == 1  # 1 order executed
        remaining = engine.get_cash("t_tr_b")
        # Buy costs: principal + commission. Should be < 50000.
        assert 48000 < remaining < 49500  # ~49000 after expenses
        # Position recorded
        positions = engine.get_positions("t_tr_b")
        assert len(positions) == 1
        assert positions[0]["symbol"] == "TICKER1"

    def test_sell_increases_cash(self):
        engine = ExecutionEngine()
        engine.set_initial_capital("t_tr_s", 50000)
        engine.execute(
            [Order(symbol="TKR_SELL", side="buy", shares=200, price=10.0, cost=0)],
            "2026-07-01", "t_tr_s"
        )
        precash = engine.get_cash("t_tr_s")
        engine.execute(
            [Order(symbol="TKR_SELL", side="sell", shares=100, price=12.0, cost=0)],
            "2026-07-09", "t_tr_s"
        )
        postcash = engine.get_cash("t_tr_s")
        # After selling at higher price, cash should increase
        assert postcash > precash

    def test_positions_tracking(self):
        engine = ExecutionEngine()
        engine.set_initial_capital("t_tr_p", 100000)
        engine.execute(
            [Order(symbol="FAKE01", side="buy", shares=300, price=5.0, cost=0),
             Order(symbol="FAKE02", side="buy", shares=500, price=8.0, cost=0)],
            "2026-07-09", "t_tr_p"
        )
        positions = engine.get_positions("t_tr_p")
        assert len(positions) == 2
        syms = {p["symbol"] for p in positions}
        assert "FAKE01" in syms
        assert "FAKE02" in syms

    def test_sell_all_reduces_position_to_zero(self):
        engine = ExecutionEngine()
        engine.set_initial_capital("t_tr_z", 50000)
        engine.execute(
            [Order(symbol="FULL01", side="buy", shares=100, price=10.0, cost=0)],
            "2026-07-01", "t_tr_z"
        )
        engine.execute(
            [Order(symbol="FULL01", side="sell", shares=100, price=12.0, cost=0)],
            "2026-07-09", "t_tr_z"
        )
        positions = engine.get_positions("t_tr_z")
        assert len(positions) == 0

    def test_get_trades_records(self):
        engine = ExecutionEngine()
        engine.set_initial_capital("t_tr_r", 100000)
        engine.execute(
            [Order(symbol="REC01", side="buy", shares=100, price=5.0, cost=0)],
            "2026-07-09", "t_tr_r"
        )
        trades = engine.get_trades("t_tr_r", limit=10)
        assert len(trades) >= 1
        assert any(t["symbol"] == "REC01" for t in trades)
