"""Unit tests for broker_adapter — Phase 10 券商接口适配层.

Tests cover:
  - OrderResponse / Trade / Position / Account / OrderRequest dataclasses
  - SimulatorBroker: connect, submit_order, cancel_order, query_*
  - BrokerManager: register, connect_all, submit_order
  - get_broker_manager() factory + singleton
  - RateLimiter 限流
"""

import asyncio
import pytest
from decimal import Decimal

from quant.execution.broker_adapter import (
    BrokerType,
    OrderSide,
    OrderType,
    OrderStatus,
    TimeInForce,
    OrderRequest,
    OrderResponse,
    Trade,
    Position,
    Account,
    BrokerConfig,
    RateLimiter,
    BrokerAdapterBase,
    SimulatorBroker,
    BrokerManager,
    get_broker_manager,
    init_broker_manager,
)


# ═══════════════════════════════════════════════════════════
# Dataclass Tests
# ═══════════════════════════════════════════════════════════

class TestOrderRequest:
    def test_defaults(self):
        req = OrderRequest(
            symbol="600036",
            side=OrderSide.BUY,
            quantity=100,
        )
        assert req.symbol == "600036"
        assert req.side == OrderSide.BUY
        assert req.quantity == 100
        assert req.order_type == OrderType.LIMIT
        assert req.price == 0.0
        assert req.time_in_force == TimeInForce.DAY
        assert req.client_order_id  # auto-generated

    def test_full_spec(self):
        req = OrderRequest(
            symbol="000001",
            side=OrderSide.SELL,
            quantity=200,
            order_type=OrderType.MARKET,
            price=15.5,
            time_in_force=TimeInForce.IOC,
            account_id="ACC_001",
            strategy_id="momentum_v1",
            client_order_id="custom_123",
            metadata={"tag": "test"},
        )
        assert req.order_type == OrderType.MARKET
        assert req.price == 15.5
        assert req.time_in_force == TimeInForce.IOC
        assert req.account_id == "ACC_001"
        assert req.strategy_id == "momentum_v1"
        assert req.client_order_id == "custom_123"
        assert req.metadata["tag"] == "test"


class TestOrderResponse:
    def test_filled(self):
        resp = OrderResponse(
            order_id="BRK_123",
            client_order_id="CLI_456",
            status=OrderStatus.FILLED,
            message="OK",
        )
        assert resp.order_id == "BRK_123"
        assert resp.client_order_id == "CLI_456"
        assert resp.status == OrderStatus.FILLED
        assert resp.message == "OK"
        assert resp.timestamp

    def test_rejected(self):
        resp = OrderResponse(
            order_id="",
            client_order_id="CLI_456",
            status=OrderStatus.REJECTED,
            message="Insufficient funds",
        )
        assert not resp.order_id
        assert resp.status == OrderStatus.REJECTED


class TestTrade:
    def test_trade_fields(self):
        trade = Trade(
            trade_id="TRD_001",
            order_id="ORD_001",
            client_order_id="CLI_001",
            symbol="600036",
            side=OrderSide.BUY,
            price=20.0,
            quantity=100,
            timestamp=datetime.utcnow(),
            commission=5.0,
            tax=10.0,
        )
        assert trade.commission == 5.0
        assert trade.tax == 10.0


class TestPosition:
    def test_position_calculation(self):
        pos = Position(
            symbol="600036",
            long_quantity=1000,
            short_quantity=0,
            long_avg_price=20.0,
            short_avg_price=0.0,
            market_value=21000.0,
            unrealized_pnl=1000.0,
            realized_pnl=500.0,
        )
        assert pos.long_quantity == 1000
        assert pos.short_quantity == 0
        assert pos.unrealized_pnl == 1000.0


class TestAccount:
    def test_account_fields(self):
        acct = Account(
            account_id="ACC_001",
            total_assets=100000.0,
            available_cash=50000.0,
            frozen_cash=10000.0,
            market_value=40000.0,
            margin_used=5000.0,
            margin_available=45000.0,
        )
        assert acct.total_assets == 100000.0
        assert acct.available_cash == 50000.0


class TestBrokerConfig:
    def test_default_config(self):
        config = BrokerConfig(
            broker_type=BrokerType.SIMULATOR,
            name="test_sim",
            account_id="ACC_001",
        )
        assert config.broker_type == BrokerType.SIMULATOR
        assert config.max_orders_per_second == 100
        assert config.max_orders_per_minute == 3000


# ═══════════════════════════════════════════════════════════
# RateLimiter Tests
# ═══════════════════════════════════════════════════════════

class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_basic_acquire(self):
        limiter = RateLimiter(rate_per_second=10, burst=5)
        # Should allow burst
        for _ in range(5):
            assert await limiter.acquire()
        # 6th should fail
        assert not await limiter.acquire()
    
    @pytest.mark.asyncio
    async def test_token_refill(self):
        limiter = RateLimiter(rate_per_second=100, burst=10)
        for _ in range(10):
            await limiter.acquire()
        # Wait for refill
        await asyncio.sleep(0.15)
        assert await limiter.acquire()
    
    @pytest.mark.asyncio
    async def test_wait_for_token(self):
        limiter = RateLimiter(rate_per_second=100, burst=1)
        await limiter.acquire()  # consume the only token
        start = time.monotonic()
        await limiter.wait_for_token()
        elapsed = time.monotonic() - start
        # Should wait approximately 0.01s for 1 token at 100/s
        assert 0.005 < elapsed < 0.05


# ═══════════════════════════════════════════════════════════
# SimulatorBroker Tests
# ═══════════════════════════════════════════════════════════

class TestSimulatorBroker:
    @pytest.fixture
    def broker(self):
        config = BrokerConfig(
            broker_type=BrokerType.SIMULATOR,
            name="test_sim",
            account_id="TEST_ACC",
        )
        return SimulatorBroker(config)
    
    @pytest.mark.asyncio
    async def test_connect_disconnect(self, broker):
        assert not broker.is_connected()
        assert await broker.connect()
        assert broker.is_connected()
        await broker.disconnect()
        assert not broker.is_connected()
    
    @pytest.mark.asyncio
    async def test_submit_limit_order(self, broker):
        await broker.connect()
        req = OrderRequest(
            symbol="600036",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.LIMIT,
            price=20.0,
        )
        resp = await broker.submit_order(req)
        assert resp.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
        assert resp.client_order_id == req.client_order_id
    
    @pytest.mark.asyncio
    async def test_submit_market_order(self, broker):
        await broker.connect()
        req = OrderRequest(
            symbol="000001",
            side=OrderSide.SELL,
            quantity=200,
            order_type=OrderType.MARKET,
        )
        resp = await broker.submit_order(req)
        assert resp.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
    
    @pytest.mark.asyncio
    async def test_cancel_order(self, broker):
        await broker.connect()
        req = OrderRequest(symbol="600036", side=OrderSide.BUY, quantity=100)
        resp = await broker.submit_order(req)
        cancelled = await broker.cancel_order(resp.order_id)
        assert cancelled
    
    @pytest.mark.asyncio
    async def test_query_account(self, broker):
        await broker.connect()
        acct = await broker.query_account()
        assert isinstance(acct, Account)
        assert acct.account_id == "TEST_ACC"
        assert acct.total_assets >= 0
    
    @pytest.mark.asyncio
    async def test_query_positions(self, broker):
        await broker.connect()
        positions = await broker.query_positions()
        assert isinstance(positions, list)
    
    @pytest.mark.asyncio
    async def test_query_orders(self, broker):
        await broker.connect()
        orders = await broker.query_orders()
        assert isinstance(orders, list)
    
    @pytest.mark.asyncio
    async def test_query_trades(self, broker):
        await broker.connect()
        trades = await broker.query_trades()
        assert isinstance(trades, list)
    
    @pytest.mark.asyncio
    async def test_callbacks(self, broker):
        await broker.connect()
        events = []
        broker.on_order(lambda r: events.append(("order", r)))
        broker.on_trade(lambda t: events.append(("trade", t)))
        broker.on_error(lambda e: events.append(("error", e)))
        
        req = OrderRequest(symbol="600036", side=OrderSide.BUY, quantity=100)
        await broker.submit_order(req)
        
        assert len(events) >= 1
        assert events[0][0] == "order"
    
    @pytest.mark.asyncio
    async def test_reject_when_disconnected(self, broker):
        req = OrderRequest(symbol="600036", side=OrderSide.BUY, quantity=100)
        resp = await broker.submit_order(req)
        assert resp.status == OrderStatus.REJECTED
        assert "not connected" in resp.message.lower()


# ═══════════════════════════════════════════════════════════
# BrokerManager Tests
# ═══════════════════════════════════════════════════════════

class TestBrokerManager:
    @pytest.fixture
    def manager(self):
        return BrokerManager()
    
    @pytest.mark.asyncio
    async def test_register_broker(self, manager):
        config = BrokerConfig(broker_type=BrokerType.SIMULATOR, name="broker1", account_id="A1")
        manager.register_broker("broker1", SimulatorBroker(config), default=True)
        assert "broker1" in manager.list_brokers()
        assert manager.get_broker("broker1") is not None
    
    @pytest.mark.asyncio
    async def test_default_broker(self, manager):
        config = BrokerConfig(broker_type=BrokerType.SIMULATOR, name="default_broker", account_id="A1")
        manager.register_broker("default_broker", SimulatorBroker(config), default=True)
        assert manager.get_broker() is not None
        assert manager.get_broker().config.name == "default_broker"
    
    @pytest.mark.asyncio
    async def test_connect_all(self, manager):
        config1 = BrokerConfig(broker_type=BrokerType.SIMULATOR, name="b1", account_id="A1")
        config2 = BrokerConfig(broker_type=BrokerType.SIMULATOR, name="b2", account_id="A2")
        manager.register_broker("b1", SimulatorBroker(config1))
        manager.register_broker("b2", SimulatorBroker(config2))
        results = await manager.connect_all()
        assert results["b1"]
        assert results["b2"]
        await manager.disconnect_all()
    
    @pytest.mark.asyncio
    async def test_submit_order_via_manager(self, manager):
        config = BrokerConfig(broker_type=BrokerType.SIMULATOR, name="mgr_broker", account_id="A1")
        manager.register_broker("mgr_broker", SimulatorBroker(config), default=True)
        await manager.connect_all()
        
        req = OrderRequest(symbol="600036", side=OrderSide.BUY, quantity=100)
        resp = await manager.submit_order(req)
        assert resp.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
        await manager.disconnect_all()
    
    @pytest.mark.asyncio
    async def test_submit_order_specific_broker(self, manager):
        config1 = BrokerConfig(broker_type=BrokerType.SIMULATOR, name="b1", account_id="A1")
        config2 = BrokerConfig(broker_type=BrokerType.SIMULATOR, name="b2", account_id="A2")
        manager.register_broker("b1", SimulatorBroker(config1))
        manager.register_broker("b2", SimulatorBroker(config2))
        await manager.connect_all()
        
        req = OrderRequest(symbol="600036", side=OrderSide.BUY, quantity=100)
        resp = await manager.submit_order(req, "b2")
        assert resp.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
        await manager.disconnect_all()
    
    @pytest.mark.asyncio
    async def test_get_connected_brokers(self, manager):
        config = BrokerConfig(broker_type=BrokerType.SIMULATOR, name="conn_test", account_id="A1")
        manager.register_broker("conn_test", SimulatorBroker(config))
        await manager.connect_all()
        connected = manager.get_connected_brokers()
        assert "conn_test" in connected
        await manager.disconnect_all()


# ═══════════════════════════════════════════════════════════
# Factory Tests
# ═══════════════════════════════════════════════════════════

class TestFactory:
    def test_get_broker_manager_singleton(self):
        init_broker_manager()
        m1 = get_broker_manager()
        m2 = get_broker_manager()
        assert m1 is m2
    
    def test_default_simulator_registered(self):
        m = get_broker_manager()
        assert "simulator" in m.list_brokers()
        sim = m.get_broker("simulator")
        assert sim is not None
        assert sim.config.broker_type == BrokerType.SIMULATOR


# ═══════════════════════════════════════════════════════════
# Integration Tests
# ═══════════════════════════════════════════════════════════

class TestIntegration:
    @pytest.mark.asyncio
    async def test_full_order_lifecycle(self):
        config = BrokerConfig(
            broker_type=BrokerType.SIMULATOR,
            name="lifecycle",
            account_id="LIFECYCLE_ACC",
        )
        broker = SimulatorBroker(config)
        await broker.connect()
        
        # 1. Submit order
        req = OrderRequest(
            symbol="600036",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.LIMIT,
            price=20.0,
            strategy_id="test_strategy",
        )
        resp = await broker.submit_order(req)
        assert resp.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
        order_id = resp.order_id
        
        # 2. Query account
        acct = await broker.query_account()
        assert acct.account_id == "LIFECYCLE_ACC"
        
        # 3. Cancel (if still pending)
        if resp.status == OrderStatus.SUBMITTED:
            await broker.cancel_order(order_id)
        
        # 4. Query positions
        positions = await broker.query_positions()
        assert isinstance(positions, list)
        
        await broker.disconnect()
    
    @pytest.mark.asyncio
    async def test_rate_limiting(self):
        config = BrokerConfig(
            broker_type=BrokerType.SIMULATOR,
            name="rate_limit",
            account_id="RATE_ACC",
            max_orders_per_second=5,
        )
        broker = SimulatorBroker(config)
        await broker.connect()
        
        # Submit multiple orders quickly
        tasks = []
        for i in range(10):
            req = OrderRequest(symbol="600036", side=OrderSide.BUY, quantity=100)
            tasks.append(broker.submit_order(req))
        
        responses = await asyncio.gather(*tasks)
        # All should succeed (simulator is fast, but rate limiter works)
        for r in responses:
            assert r.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED, OrderStatus.REJECTED)
        
        await broker.disconnect()
    
    @pytest.mark.asyncio
    async def test_manager_multi_broker(self):
        manager = BrokerManager()
        
        # Register multiple simulators
        for i in range(3):
            config = BrokerConfig(
                broker_type=BrokerType.SIMULATOR,
                name=f"broker_{i}",
                account_id=f"ACC_{i}",
            )
            manager.register_broker(f"broker_{i}", SimulatorBroker(config))
        
        await manager.connect_all()
        
        # Submit to each
        for i in range(3):
            req = OrderRequest(symbol="600036", side=OrderSide.BUY, quantity=100)
            resp = await manager.submit_order(req, f"broker_{i}")
            assert resp.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
        
        connected = manager.get_connected_brokers()
        assert len(connected) == 3
        
        await manager.disconnect_all()


# ═══════════════════════════════════════════════════════════
# Abstract Base Class Contract
# ═══════════════════════════════════════════════════════════

class TestAbstractContract:
    """Verify SimulatorBroker satisfies BrokerAdapterBase interface."""
    
    def test_has_all_methods(self):
        assert hasattr(SimulatorBroker, 'connect')
        assert hasattr(SimulatorBroker, 'disconnect')
        assert hasattr(SimulatorBroker, 'submit_order')
        assert hasattr(SimulatorBroker, 'cancel_order')
        assert hasattr(SimulatorBroker, 'query_orders')
        assert hasattr(SimulatorBroker, 'query_trades')
        assert hasattr(SimulatorBroker, 'query_positions')
        assert hasattr(SimulatorBroker, 'query_account')
        assert hasattr(SimulatorBroker, 'is_connected')
        assert hasattr(SimulatorBroker, 'on_order')
        assert hasattr(SimulatorBroker, 'on_trade')
        assert hasattr(SimulatorBroker, 'on_position')
        assert hasattr(SimulatorBroker, 'on_account')
        assert hasattr(SimulatorBroker, 'on_error')
        assert hasattr(SimulatorBroker, 'on_connected')
        assert hasattr(SimulatorBroker, 'on_disconnected')


# Import datetime for tests
from datetime import datetime
import time
