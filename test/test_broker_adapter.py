"""Unit tests for broker_adapter — ADR-036 vnpy integration.

Tests cover:
  - OrderResult / AccountInfo dataclasses
  - SimulatedAdapter: connect, buy, sell, get_account, get_positions
  - VnpyAdapter: graceful fallback when vnpy not installed
  - get_broker_adapter() factory + singleton
  - Reset lifecycle
"""

import os
import tempfile
import pytest

from quant.execution.broker_adapter import (
    BrokerAdapter,
    SimulatedAdapter,
    VnpyAdapter,
    VnpyCtpAdapter,
    VnpyXtpAdapter,
    OrderResult,
    AccountInfo,
    get_broker_adapter,
    reset_adapter,
    _check_vnpy,
)


# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _mock_engine_checks(monkeypatch):
    """Disable ex-dividend + T+1 checks in tests (use temp DB without market.db)."""
    from quant.execution import engine as _eng
    monkeypatch.setattr(_eng.ExecutionEngine, '_check_ex_dividend', lambda *a, **kw: False)
    from quant.data.repos import trade_repo as _tr
    monkeypatch.setattr(_tr.TradeRepo, 'check_t1', lambda *a, **kw: False)


@pytest.fixture
def temp_trades_db():
    """Create a temporary trades.db with proper schema for testing."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_broker_")
    os.close(fd)

    # Let TradeRepo/ExecutionEngine auto-create schema
    from quant.data.repos import TradeRepo
    repo = TradeRepo(db_path=path)
    repo.set_initial_capital("test", 50000)

    yield path
    os.unlink(path)


@pytest.fixture
def sim_adapter(temp_trades_db):
    """Create a connected SimulatedAdapter with clean temp DB (¥50K capital)."""
    adapter = SimulatedAdapter(db_path=temp_trades_db, strategy="test")
    adapter.connect()
    return adapter


# ═══════════════════════════════════════════════════════════
# Dataclass tests
# ═══════════════════════════════════════════════════════════

class TestOrderResult:
    def test_success_order(self):
        r = OrderResult(
            success=True, symbol="600036", side="buy",
            shares=100, price=20.0, filled_shares=100,
            filled_price=20.0, status="filled", is_simulated=True,
        )
        assert r.success
        assert r.symbol == "600036"
        assert r.side == "buy"
        assert r.status == "filled"
        assert r.is_simulated

    def test_failed_order(self):
        r = OrderResult(
            success=False, symbol="000001", side="sell",
            shares=200, price=15.0,
            error="insufficient cash", is_simulated=False,
        )
        assert not r.success
        assert r.error == "insufficient cash"
        assert r.status == ""

    def test_defaults(self):
        r = OrderResult(success=True)
        assert r.shares == 0
        assert r.price == 0.0
        assert r.filled_shares == 0
        assert r.status == ""
        assert not r.is_simulated


class TestAccountInfo:
    def test_empty(self):
        a = AccountInfo()
        assert a.total_asset == 0.0
        assert a.available_cash == 0.0
        assert a.positions == []

    def test_with_positions(self):
        a = AccountInfo(
            total_asset=10000.0,
            available_cash=3000.0,
            frozen_cash=500.0,
            positions=[{"symbol": "600036", "shares": 100, "market_value": 7000.0}],
        )
        assert a.total_asset == 10000.0
        assert a.available_cash == 3000.0
        assert a.frozen_cash == 500.0
        assert len(a.positions) == 1


# ═══════════════════════════════════════════════════════════
# SimulatedAdapter tests
# ═══════════════════════════════════════════════════════════

class TestSimulatedAdapterLifecycle:
    def test_connect_disconnect(self, temp_trades_db):
        adapter = SimulatedAdapter(db_path=temp_trades_db)
        assert not adapter.is_connected()
        assert adapter.connect()
        assert adapter.is_connected()
        adapter.disconnect()
        assert not adapter.is_connected()

    def test_name(self, sim_adapter):
        assert sim_adapter.name == "simulated"

    def test_auto_connect_on_operation(self, temp_trades_db):
        adapter = SimulatedAdapter(db_path=temp_trades_db)
        # get_account should auto-connect
        acct = adapter.get_account()
        assert adapter.is_connected()
        assert acct.total_asset == 0.0


class TestSimulatedAdapterBuySell:
    def test_buy_reduces_cash(self, sim_adapter):
        acct_before = sim_adapter.get_account()
        initial_cash = acct_before.available_cash

        result = sim_adapter.buy("600036", 20.0, 100, order_type="LIMIT")
        assert result.success, f"Buy failed: {result.error}"
        assert result.symbol == "600036"
        assert result.side == "buy"
        assert result.status == "filled"
        assert result.is_simulated

        acct_after = sim_adapter.get_account()
        assert acct_after.available_cash < initial_cash

    def test_sell_increases_cash(self, sim_adapter):
        # First buy to have a position
        sim_adapter.buy("600036", 20.0, 100)
        acct_before = sim_adapter.get_account()
        cash_before = acct_before.available_cash

        result = sim_adapter.sell("600036", 22.0, 100, order_type="MARKET")
        assert result.success, f"Sell failed: {result.error}"
        assert result.symbol == "600036"

        acct_after = sim_adapter.get_account()
        assert acct_after.available_cash > cash_before

    def test_buy_insufficient_cash(self, sim_adapter):
        # Try to buy more than available cash
        result = sim_adapter.buy("600036", 99999.0, 10000)
        assert not result.success
        assert "insufficient" in result.error.lower()

    def test_positions_tracking(self, sim_adapter):
        sim_adapter.buy("600036", 20.0, 100)
        positions = sim_adapter.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "600036"
        assert positions[0]["shares"] == 100

    def test_buy_sell_zero_position(self, sim_adapter):
        sim_adapter.buy("600036", 20.0, 100)
        sim_adapter.sell("600036", 22.0, 100)
        positions = sim_adapter.get_positions()
        # After selling all, position should be zero
        remaining = sum(p["shares"] for p in positions)
        assert remaining == 0

    def test_multiple_symbols(self, sim_adapter):
        sim_adapter.buy("600036", 20.0, 100)
        sim_adapter.buy("000001", 15.0, 200)

        positions = sim_adapter.get_positions()
        syms = {p["symbol"] for p in positions}
        assert "600036" in syms
        assert "000001" in syms


class TestSimulatedAdapterAccount:
    def test_get_account(self, sim_adapter):
        acct = sim_adapter.get_account()
        assert isinstance(acct, AccountInfo)
        assert acct.total_asset >= 0
        assert acct.available_cash >= 0

    def test_get_orders(self, sim_adapter):
        sim_adapter.buy("600036", 20.0, 100)
        orders = sim_adapter.get_orders()
        assert len(orders) >= 1

    def test_cancel_is_noop(self, sim_adapter):
        # cancel should always return True in simulated mode
        assert sim_adapter.cancel("fake_order_id")


# ═══════════════════════════════════════════════════════════
# VnpyAdapter tests (graceful when vnpy not installed)
# ═══════════════════════════════════════════════════════════

class TestVnpyAdapterGraceful:
    def test_vnpy_detection(self):
        available = _check_vnpy()
        # Should return False in test env (no vnpy installed)
        assert isinstance(available, bool)

    def test_vnpy_adapter_creation_no_crash(self):
        adapter = VnpyAdapter()
        assert adapter.name == "vnpy"
        assert not adapter._vnpy_available or isinstance(adapter._vnpy_available, bool)

    def test_vnpy_ctp_creation(self):
        adapter = VnpyCtpAdapter()
        assert adapter.name == "vnpy_ctp"
        assert not adapter.is_connected()

    def test_vnpy_xtp_creation(self):
        adapter = VnpyXtpAdapter()
        assert adapter.name == "vnpy_xtp"

    def test_vnpy_connect_raises_when_not_installed(self):
        adapter = VnpyAdapter()
        if not adapter._vnpy_available:
            with pytest.raises(RuntimeError, match="vnpy not installed"):
                adapter.connect()

    def test_vnpy_disconnect_no_crash(self):
        adapter = VnpyAdapter()
        adapter.disconnect()  # Should not raise

    def test_vnpy_buy_not_connected(self):
        adapter = VnpyAdapter()
        result = adapter.buy("600036", 20.0, 100)
        assert not result.success
        assert "not connected" in result.error.lower()

    def test_vnpy_sell_not_connected(self):
        adapter = VnpyAdapter()
        result = adapter.sell("600036", 20.0, 100)
        assert not result.success
        assert "not connected" in result.error.lower()

    def test_symbol_conversion(self):
        adapter = VnpyAdapter()
        assert adapter._symbol_to_vnpy("600036") == "SSE.600036"
        assert adapter._symbol_to_vnpy("000001") == "SZSE.000001"
        assert adapter._symbol_to_vnpy("688001") == "SSE.688001"
        assert adapter._symbol_to_vnpy("300750") == "SZSE.300750"
        assert adapter._symbol_to_vnpy("430001") == "BSE.430001"
        assert adapter._symbol_to_vnpy("920001") == "BSE.920001"

    def test_symbol_from_vnpy(self):
        adapter = VnpyAdapter()
        assert adapter._symbol_from_vnpy("SSE.600036") == "600036"
        assert adapter._symbol_from_vnpy("SZSE.000001") == "000001"
        assert adapter._symbol_from_vnpy("BSE.430001") == "430001"


# ═══════════════════════════════════════════════════════════
# Factory + Singleton tests
# ═══════════════════════════════════════════════════════════

class TestFactory:
    def test_get_broker_adapter_default_simulated(self, temp_trades_db):
        reset_adapter()
        # Override db_path for this test
        adapter = get_broker_adapter("simulated", db_path=temp_trades_db)
        assert isinstance(adapter, SimulatedAdapter)
        assert adapter.name == "simulated"
        assert adapter.is_connected()

    def test_get_broker_adapter_singleton(self, temp_trades_db):
        reset_adapter()
        a1 = get_broker_adapter("simulated", db_path=temp_trades_db)
        a2 = get_broker_adapter()
        assert a1 is a2  # Same instance

    def test_reset_adapter(self, temp_trades_db):
        reset_adapter()
        a1 = get_broker_adapter("simulated", db_path=temp_trades_db)
        assert a1.is_connected()
        reset_adapter()
        # After reset, new adapter should be created
        a2 = get_broker_adapter("simulated", db_path=temp_trades_db)
        assert a2.is_connected()
        # May or may not be same object (singleton cleared)

    def test_unknown_adapter_falls_back(self, temp_trades_db):
        reset_adapter()
        adapter = get_broker_adapter("nonexistent", db_path=temp_trades_db)
        assert isinstance(adapter, SimulatedAdapter)

    def test_vnpy_ctp_factory(self, temp_trades_db):
        reset_adapter()
        adapter = get_broker_adapter("vnpy_ctp", db_path=temp_trades_db)
        assert isinstance(adapter, VnpyCtpAdapter)

    def test_vnpy_xtp_factory(self, temp_trades_db):
        reset_adapter()
        adapter = get_broker_adapter("vnpy_xtp", db_path=temp_trades_db)
        assert isinstance(adapter, VnpyXtpAdapter)


# ═══════════════════════════════════════════════════════════
# Abstract base class contract
# ═══════════════════════════════════════════════════════════

class TestAbstractContract:
    """Verify all concrete implementations satisfy the abstract interface."""

    def test_simulated_has_all_methods(self):
        assert hasattr(SimulatedAdapter, 'connect')
        assert hasattr(SimulatedAdapter, 'disconnect')
        assert hasattr(SimulatedAdapter, 'buy')
        assert hasattr(SimulatedAdapter, 'sell')
        assert hasattr(SimulatedAdapter, 'cancel')
        assert hasattr(SimulatedAdapter, 'get_positions')
        assert hasattr(SimulatedAdapter, 'get_account')
        assert hasattr(SimulatedAdapter, 'get_orders')
        assert hasattr(SimulatedAdapter, 'is_connected')

    def test_vnpy_has_all_methods(self):
        assert hasattr(VnpyAdapter, 'connect')
        assert hasattr(VnpyAdapter, 'disconnect')
        assert hasattr(VnpyAdapter, 'buy')
        assert hasattr(VnpyAdapter, 'sell')
        assert hasattr(VnpyAdapter, 'cancel')
        assert hasattr(VnpyAdapter, 'get_positions')
        assert hasattr(VnpyAdapter, 'get_account')
        assert hasattr(VnpyAdapter, 'get_orders')
        assert hasattr(VnpyAdapter, 'is_connected')
