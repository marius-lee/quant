"""券商接口适配层 - 统一 Order/Trade/Position/Account 接口."""

from __future__ import annotations
import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

logger = get_logger("execution.broker_adapter")


class BrokerType(Enum):
    """券商类型."""
    CTP = "ctp"
    XTQUANT = "xtquant"
    HONGSU = "hongsu"
    ZHONGXIN = "zhongxin"
    GUOJIN = "guojin"
    SIMULATOR = "simulator"


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    FAK = "fak"
    FOK = "fok"


class OrderStatus(Enum):
    PENDING_NEW = "pending_new"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    PENDING_CANCEL = "pending_cancel"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TimeInForce(Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


@dataclass
class OrderRequest:
    """下单请求."""
    symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0
    time_in_force: TimeInForce = TimeInForce.DAY
    account_id: str = ""
    strategy_id: str = ""
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4())[:16])
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResponse:
    """下单响应."""
    order_id: str
    client_order_id: str
    status: OrderStatus
    message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Trade:
    """成交回报."""
    trade_id: str
    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    price: float
    quantity: float
    timestamp: datetime
    commission: float = 0.0
    tax: float = 0.0


@dataclass
class Position:
    """持仓."""
    symbol: str
    long_quantity: float = 0.0
    short_quantity: float = 0.0
    long_avg_price: float = 0.0
    short_avg_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    update_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Account:
    """账户资金."""
    account_id: str
    total_assets: float = 0.0
    available_cash: float = 0.0
    frozen_cash: float = 0.0
    market_value: float = 0.0
    margin_used: float = 0.0
    margin_available: float = 0.0
    update_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BrokerConfig:
    """券商配置."""
    broker_type: BrokerType
    name: str
    api_key: str = ""
    api_secret: str = ""
    app_id: str = ""
    auth_code: str = ""
    front_address: str = ""
    account_id: str = ""
    password: str = ""
    max_orders_per_second: int = 100
    max_orders_per_minute: int = 3000
    reconnect_interval: int = 5
    max_reconnect_attempts: int = 10
    heartbeat_interval: int = 30


class RateLimiter:
    """令牌桶限流器."""
    
    def __init__(self, rate_per_second: int, burst: int = None):
        self.rate = rate_per_second
        self.burst = burst or rate_per_second
        self._tokens = float(self.burst)
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()
    
    async def acquire(self, tokens: int = 1) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_update
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
            self._last_update = now
            
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False
    
    async def wait_for_token(self, tokens: int = 1):
        while not await self.acquire(tokens):
            await asyncio.sleep(0.01)


class BrokerAdapterBase(ABC):
    """券商适配器基类."""
    
    def __init__(self, config: BrokerConfig):
        self.config = config
        self._connected = False
        self._running = False
        self._callbacks: Dict[str, List[Callable]] = {
            "on_order": [],
            "on_trade": [],
            "on_position": [],
            "on_account": [],
            "on_error": [],
            "on_connected": [],
            "on_disconnected": [],
        }
        self._order_map: Dict[str, OrderRequest] = {}
        self._pending_orders: Dict[str, asyncio.Future] = {}
        self._rate_limiter = RateLimiter(config.max_orders_per_second, config.max_orders_per_minute // 60)
        self._reconnect_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
    
    # ═══════════════════════════════════════════════════════════
    # 回调注册
    # ═════════════════════════════════════════════════════════
    
    def on_order(self, callback: Callable[[OrderResponse], None]):
        self._callbacks["on_order"].append(callback)
    
    def on_trade(self, callback: Callable[[Trade], None]):
        self._callbacks["on_trade"].append(callback)
    
    def on_position(self, callback: Callable[[Position], None]):
        self._callbacks["on_position"].append(callback)
    
    def on_account(self, callback: Callable[[Account], None]):
        self._callbacks["on_account"].append(callback)
    
    def on_error(self, callback: Callable[[Exception], None]):
        self._callbacks["on_error"].append(callback)
    
    def on_connected(self, callback: Callable[[], None]):
        self._callbacks["on_connected"].append(callback)
    
    def on_disconnected(self, callback: Callable[[], None]):
        self._callbacks["on_disconnected"].append(callback)
    
    def _emit(self, event: str, *args, **kwargs):
        for cb in self._callbacks.get(event, []):
            try:
                cb(*args, **kwargs)
            except Exception as e:
                logger.error(f"Callback {cb} error: {e}")
    
    # ═══════════════════════════════════════════════════════════
    # 抽象方法 - 子类必须实现
    # ══════════════════════════════════════════════════════════
    
    @abstractmethod
    async def _connect_impl(self) -> bool:
        pass
    
    @abstractmethod
    async def _disconnect_impl(self):
        pass
    
    @abstractmethod
    async def _submit_order_impl(self, request: OrderRequest) -> OrderResponse:
        pass
    
    @abstractmethod
    async def _cancel_order_impl(self, order_id: str) -> bool:
        pass
    
    @abstractmethod
    async def _query_orders_impl(self) -> List[OrderResponse]:
        pass
    
    @abstractmethod
    async def _query_trades_impl(self) -> List[Trade]:
        pass
    
    @abstractmethod
    async def _query_positions_impl(self) -> List[Position]:
        pass
    
    @abstractmethod
    async def _query_account_impl(self) -> Account:
        pass
    
    # ══════════════════════════════════════════════════════════
    # 公共接口
    # ══════════════════════════════════════════════════════════
    
    async def connect(self) -> bool:
        if self._connected:
            return True
        
        try:
            success = await self._connect_impl()
            if success:
                self._connected = True
                self._running = True
                self._start_background_tasks()
                self._emit("on_connected")
                logger.info(f"Broker {self.config.name} connected")
            return success
        except Exception as e:
            logger.error(f"Broker {self.config.name} connect failed: {e}")
            self._emit("on_error", e)
            return False
    
    async def disconnect(self):
        self._running = False
        self._stop_background_tasks()
        await self._disconnect_impl()
        self._connected = False
        self._emit("on_disconnected")
        logger.info(f"Broker {self.config.name} disconnected")
    
    def _start_background_tasks(self):
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
    
    def _stop_background_tasks(self):
        for task in [self._reconnect_task, self._heartbeat_task]:
            if task and not task.done():
                task.cancel()
    
    async def _reconnect_loop(self):
        while self._running:
            if not self._connected:
                logger.warning(f"Broker {self.config.name} disconnected, attempting reconnect...")
                for attempt in range(self.config.max_reconnect_attempts):
                    if await self.connect():
                        break
                    await asyncio.sleep(self.config.reconnect_interval)
            await asyncio.sleep(10)
    
    async def _heartbeat_loop(self):
        while self._running:
            if self._connected:
                try:
                    await self._query_account_impl()
                except Exception as e:
                    logger.warning(f"Heartbeat failed: {e}")
                    self._connected = False
            await asyncio.sleep(self.config.heartbeat_interval)
    
    async def submit_order(self, request: OrderRequest) -> OrderResponse:
        await self._rate_limiter.wait_for_token()
        
        if not self._connected:
            return OrderResponse(
                order_id="",
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="Broker not connected"
            )
        
        self._order_map[request.client_order_id] = request
        
        try:
            response = await self._submit_order_impl(request)
            self._emit("on_order", response)
            return response
        except Exception as e:
            logger.error(f"Submit order failed: {e}")
            self._emit("on_error", e)
            return OrderResponse(
                order_id="",
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message=str(e)
            )
    
    async def cancel_order(self, order_id: str) -> bool:
        if not self._connected:
            return False
        try:
            result = await self._cancel_order_impl(order_id)
            return result
        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            self._emit("on_error", e)
            return False
    
    async def query_orders(self) -> List[OrderResponse]:
        if not self._connected:
            return []
        return await self._query_orders_impl()
    
    async def query_trades(self) -> List[Trade]:
        if not self._connected:
            return []
        return await self._query_trades_impl()
    
    async def query_positions(self) -> List[Position]:
        if not self._connected:
            return []
        return await self._query_positions_impl()
    
    async def query_account(self) -> Account:
        if not self._connected:
            return Account(account_id=self.config.account_id)
        return await self._query_account_impl()
    
    def is_connected(self) -> bool:
        return self._connected
    
    # ═══════════════════════════════════════════════════════════
    # 回调注册
    # ══════════════════════════════════════════════════════════
    
    def on_order(self, callback: Callable[[OrderResponse], None]):
        self._callbacks["on_order"].append(callback)
    
    def on_trade(self, callback: Callable[[Trade], None]):
        self._callbacks["on_trade"].append(callback)
    
    def on_position(self, callback: Callable[[Position], None]):
        self._callbacks["on_position"].append(callback)
    
    def on_account(self, callback: Callable[[Account], None]):
        self._callbacks["on_account"].append(callback)
    
    def on_error(self, callback: Callable[[Exception], None]):
        self._callbacks["on_error"].append(callback)
    
    def on_connected(self, callback: Callable[[], None]):
        self._callbacks["on_connected"].append(callback)
    
    def on_disconnected(self, callback: Callable[[], None]):
        self._callbacks["on_disconnected"].append(callback)
    
    def _emit(self, event: str, *args, **kwargs):
        for cb in self._callbacks.get(event, []):
            try:
                cb(*args, **kwargs)
            except Exception as e:
                logger.error(f"Callback {cb} error: {e}")
    
    # ═══════════════════════════════════════════════════════════
    # 抽象方法 - 子类必须实现
    # ══════════════════════════════════════════════════════════
    
    @abstractmethod
    async def _connect_impl(self) -> bool:
        pass
    
    @abstractmethod
    async def _disconnect_impl(self):
        pass
    
    @abstractmethod
    async def _submit_order_impl(self, request: OrderRequest) -> OrderResponse:
        pass
    
    @abstractmethod
    async def _cancel_order_impl(self, order_id: str) -> bool:
        pass
    
    @abstractmethod
    async def _query_orders_impl(self) -> List[OrderResponse]:
        pass
    
    @abstractmethod
    async def _query_trades_impl(self) -> List[Trade]:
        pass
    
    @abstractmethod
    async def _query_positions_impl(self) -> List[Position]:
        pass
    
    @abstractmethod
    async def _query_account_impl(self) -> Account:
        pass


class SimulatorBroker(BrokerAdapterBase):
    """模拟盘适配器 - 用于测试."""
    
    async def _connect_impl(self) -> bool:
        await asyncio.sleep(0.1)
        return True
    
    async def _disconnect_impl(self):
        pass
    
    async def _submit_order_impl(self, request: OrderRequest) -> OrderResponse:
        await asyncio.sleep(0.005)
        
        # 模拟盘总是成功
        status = OrderStatus.FILLED
        
        return OrderResponse(
            order_id=f"SIM_{uuid.uuid4().hex[:12]}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            status=status,
            message="Simulated"
        )
    
    async def _cancel_order_impl(self, order_id: str) -> bool:
        await asyncio.sleep(0.001)
        return True
    
    async def _query_orders_impl(self) -> List[OrderResponse]:
        return []
    
    async def _query_trades_impl(self) -> List[Trade]:
        return []
    
    async def _query_positions_impl(self) -> List[Position]:
        return []
    
    async def _query_account_impl(self) -> Account:
        return Account(
            account_id=self.config.account_id,
            total_assets=1000000.0,
            available_cash=500000.0,
            market_value=500000.0
        )


class BrokerManager:
    """券商管理器 - 统一管理多券商连接."""
    
    def __init__(self):
        self._brokers: Dict[str, BrokerAdapterBase] = {}
        self._default_broker: Optional[str] = None
        self._lock = asyncio.Lock()
    
    def register_broker(self, name: str, broker: BrokerAdapterBase, default: bool = False):
        self._brokers[name] = broker
        if default or self._default_broker is None:
            self._default_broker = name
    
    def get_broker(self, name: str = None) -> Optional[BrokerAdapterBase]:
        name = name or self._default_broker
        return self._brokers.get(name)
    
    async def connect_all(self) -> Dict[str, bool]:
        results = {}
        for name, broker in self._brokers.items():
            results[name] = await broker.connect()
        return results
    
    async def disconnect_all(self):
        for broker in self._brokers.values():
            await broker.disconnect()
    
    async def submit_order(self, request: OrderRequest, broker_name: str = None) -> OrderResponse:
        broker = self.get_broker(broker_name)
        if not broker:
            return OrderResponse(
                order_id="",
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="Broker not found"
            )
        return await broker.submit_order(request)
    
    async def cancel_order(self, order_id: str, broker_name: str = None) -> bool:
        broker = self.get_broker(broker_name)
        if not broker:
            return False
        return await broker.cancel_order(order_id)
    
    def list_brokers(self) -> List[str]:
        return list(self._brokers.keys())
    
    def get_connected_brokers(self) -> List[str]:
        return [name for name, broker in self._brokers.items() if broker.is_connected()]


# 全局实例
_broker_manager: Optional[BrokerManager] = None


def get_broker_manager() -> BrokerManager:
    global _broker_manager
    if _broker_manager is None:
        _broker_manager = BrokerManager()
        sim_config = BrokerConfig(
            broker_type=BrokerType.SIMULATOR,
            name="simulator",
            account_id="SIM_ACCOUNT",
        )
        _broker_manager.register_broker("simulator", SimulatorBroker(sim_config), default=True)
    return _broker_manager


def init_broker_manager() -> BrokerManager:
    global _broker_manager
    _broker_manager = BrokerManager()
    sim_config = BrokerConfig(
        broker_type=BrokerType.SIMULATOR,
        name="simulator",
        account_id="SIM_ACCOUNT",
    )
    _broker_manager.register_broker("simulator", SimulatorBroker(sim_config), default=True)
    return _broker_manager


# ════════════════════════════════════════════════════════════
# Enums and Dataclasses
# ═══════════════════════════════════════════════════════════

class BrokerType(Enum):
    CTP = "ctp"
    XTQUANT = "xtquant"
    HONGSU = "hongsu"
    ZHONGXIN = "zhongxin"
    GUOJIN = "guojin"
    SIMULATOR = "simulator"


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    FAK = "fak"
    FOK = "fok"


class OrderStatus(Enum):
    PENDING_NEW = "pending_new"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    PENDING_CANCEL = "pending_cancel"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TimeInForce(Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


@dataclass
class OrderRequest:
    symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0
    time_in_force: TimeInForce = TimeInForce.DAY
    account_id: str = ""
    strategy_id: str = ""
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4())[:16])
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResponse:
    order_id: str
    client_order_id: str
    symbol: str = ""
    status: OrderStatus = OrderStatus.PENDING_NEW
    message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Trade:
    trade_id: str
    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    price: float
    quantity: float
    timestamp: datetime
    commission: float = 0.0
    tax: float = 0.0


@dataclass
class Position:
    symbol: str
    long_quantity: float = 0.0
    short_quantity: float = 0.0
    long_avg_price: float = 0.0
    short_avg_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    update_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Account:
    account_id: str
    total_assets: float = 0.0
    available_cash: float = 0.0
    frozen_cash: float = 0.0
    market_value: float = 0.0
    margin_used: float = 0.0
    margin_available: float = 0.0
    update_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BrokerConfig:
    broker_type: BrokerType
    name: str
    api_key: str = ""
    api_secret: str = ""
    app_id: str = ""
    auth_code: str = ""
    front_address: str = ""
    account_id: str = ""
    password: str = ""
    max_orders_per_second: int = 100
    max_orders_per_minute: int = 3000
    reconnect_interval: int = 5
    max_reconnect_attempts: int = 10
    heartbeat_interval: int = 30


# ════════════════════════════════════════════════════════════
# RateLimiter
# ═══════════════════════════════════════════════════════════

class RateLimiter:
    def __init__(self, rate_per_second: int, burst: int = None):
        self.rate = rate_per_second
        self.burst = burst or rate_per_second
        self._tokens = float(self.burst)
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()
    
    async def acquire(self, tokens: int = 1) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_update
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
            self._last_update = now
            
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False
    
    async def wait_for_token(self, tokens: int = 1):
        while not await self.acquire(tokens):
            await asyncio.sleep(0.01)


# ════════════════════════════════════════════════════════════
# Abstract Base Class
# ═══════════════════════════════════════════════════════════

class BrokerAdapterBase(ABC):
    def __init__(self, config: BrokerConfig):
        self.config = config
        self._connected = False
        self._running = False
        self._callbacks: Dict[str, List[Callable]] = {
            "on_order": [],
            "on_trade": [],
            "on_position": [],
            "on_account": [],
            "on_error": [],
            "on_connected": [],
            "on_disconnected": [],
        }
        self._order_map: Dict[str, OrderRequest] = {}
        self._pending_orders: Dict[str, asyncio.Future] = {}
        self._rate_limiter = RateLimiter(config.max_orders_per_second, config.max_orders_per_minute // 60)
        self._reconnect_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
    
    # 回调注册
    def on_order(self, callback: Callable[[OrderResponse], None]):
        self._callbacks["on_order"].append(callback)
    
    def on_trade(self, callback: Callable[[Trade], None]):
        self._callbacks["on_trade"].append(callback)
    
    def on_position(self, callback: Callable[[Position], None]):
        self._callbacks["on_position"].append(callback)
    
    def on_account(self, callback: Callable[[Account], None]):
        self._callbacks["on_account"].append(callback)
    
    def on_error(self, callback: Callable[[Exception], None]):
        self._callbacks["on_error"].append(callback)
    
    def on_connected(self, callback: Callable[[], None]):
        self._callbacks["on_connected"].append(callback)
    
    def on_disconnected(self, callback: Callable[[], None]):
        self._callbacks["on_disconnected"].append(callback)
    
    def _emit(self, event: str, *args, **kwargs):
        for cb in self._callbacks.get(event, []):
            try:
                cb(*args, **kwargs)
            except Exception as e:
                logger.error(f"Callback {cb} error: {e}")
    
    # 抽象方法
    @abstractmethod
    async def _connect_impl(self) -> bool:
        pass
    
    @abstractmethod
    async def _disconnect_impl(self):
        pass
    
    @abstractmethod
    async def _submit_order_impl(self, request: OrderRequest) -> OrderResponse:
        pass
    
    @abstractmethod
    async def _cancel_order_impl(self, order_id: str) -> bool:
        pass
    
    @abstractmethod
    async def _query_orders_impl(self) -> List[OrderResponse]:
        pass
    
    @abstractmethod
    async def _query_trades_impl(self) -> List[Trade]:
        pass
    
    @abstractmethod
    async def _query_positions_impl(self) -> List[Position]:
        pass
    
    @abstractmethod
    async def _query_account_impl(self) -> Account:
        pass
    
    # 公共接口
    async def connect(self) -> bool:
        if self._connected:
            return True
        
        try:
            success = await self._connect_impl()
            if success:
                self._connected = True
                self._running = True
                self._start_background_tasks()
                self._emit("on_connected")
                logger.info(f"Broker {self.config.name} connected")
            return success
        except Exception as e:
            logger.error(f"Broker {self.config.name} connect failed: {e}")
            self._emit("on_error", e)
            return False
    
    async def disconnect(self):
        self._running = False
        self._stop_background_tasks()
        await self._disconnect_impl()
        self._connected = False
        self._emit("on_disconnected")
        logger.info(f"Broker {self.config.name} disconnected")
    
    def _start_background_tasks(self):
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
    
    def _stop_background_tasks(self):
        for task in [self._reconnect_task, self._heartbeat_task]:
            if task and not task.done():
                task.cancel()
    
    async def _reconnect_loop(self):
        while self._running:
            if not self._connected:
                logger.warning(f"Broker {self.config.name} disconnected, attempting reconnect...")
                for attempt in range(self.config.max_reconnect_attempts):
                    if await self.connect():
                        break
                    await asyncio.sleep(self.config.reconnect_interval)
            await asyncio.sleep(10)
    
    async def _heartbeat_loop(self):
        while self._running:
            if self._connected:
                try:
                    await self._query_account_impl()
                except Exception as e:
                    logger.warning(f"Heartbeat failed: {e}")
                    self._connected = False
            await asyncio.sleep(self.config.heartbeat_interval)
    
    async def submit_order(self, request: OrderRequest) -> OrderResponse:
        await self._rate_limiter.wait_for_token()
        
        if not self._connected:
            return OrderResponse(
                order_id="",
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="Broker not connected"
            )
        
        self._order_map[request.client_order_id] = request
        
        try:
            response = await self._submit_order_impl(request)
            self._emit("on_order", response)
            return response
        except Exception as e:
            logger.error(f"Submit order failed: {e}")
            self._emit("on_error", e)
            return OrderResponse(
                order_id="",
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message=str(e)
            )
    
    async def cancel_order(self, order_id: str) -> bool:
        if not self._connected:
            return False
        try:
            result = await self._cancel_order_impl(order_id)
            return result
        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            self._emit("on_error", e)
            return False
    
    async def query_orders(self) -> List[OrderResponse]:
        if not self._connected:
            return []
        return await self._query_orders_impl()
    
    async def query_trades(self) -> List[Trade]:
        if not self._connected:
            return []
        return await self._query_trades_impl()
    
    async def query_positions(self) -> List[Position]:
        if not self._connected:
            return []
        return await self._query_positions_impl()
    
    async def query_account(self) -> Account:
        if not self._connected:
            return Account(account_id=self.config.account_id)
        return await self._query_account_impl()
    
    def is_connected(self) -> bool:
        return self._connected
    
    # 回调注册
    def on_order(self, callback: Callable[[OrderResponse], None]):
        self._callbacks["on_order"].append(callback)
    
    def on_trade(self, callback: Callable[[Trade], None]):
        self._callbacks["on_trade"].append(callback)
    
    def on_position(self, callback: Callable[[Position], None]):
        self._callbacks["on_position"].append(callback)
    
    def on_account(self, callback: Callable[[Account], None]):
        self._callbacks["on_account"].append(callback)
    
    def on_error(self, callback: Callable[[Exception], None]):
        self._callbacks["on_error"].append(callback)
    
    def on_connected(self, callback: Callable[[], None]):
        self._callbacks["on_connected"].append(callback)
    
    def on_disconnected(self, callback: Callable[[], None]):
        self._callbacks["on_disconnected"].append(callback)
    
    def _emit(self, event: str, *args, **kwargs):
        for cb in self._callbacks.get(event, []):
            try:
                cb(*args, **kwargs)
            except Exception as e:
                logger.error(f"Callback {cb} error: {e}")
    
    # 抽象方法
    @abstractmethod
    async def _connect_impl(self) -> bool:
        pass
    
    @abstractmethod
    async def _disconnect_impl(self):
        pass
    
    @abstractmethod
    async def _submit_order_impl(self, request: OrderRequest) -> OrderResponse:
        pass
    
    @abstractmethod
    async def _cancel_order_impl(self, order_id: str) -> bool:
        pass
    
    @abstractmethod
    async def _query_orders_impl(self) -> List[OrderResponse]:
        pass
    
    @abstractmethod
    async def _query_trades_impl(self) -> List[Trade]:
        pass
    
    @abstractmethod
    async def _query_positions_impl(self) -> List[Position]:
        pass
    
    @abstractmethod
    async def _query_account_impl(self) -> Account:
        pass


class SimulatorBroker(BrokerAdapterBase):
    async def _connect_impl(self) -> bool:
        await asyncio.sleep(0.1)
        return True
    
    async def _disconnect_impl(self):
        pass
    
    async def _submit_order_impl(self, request: OrderRequest) -> OrderResponse:
        await asyncio.sleep(0.005)
        
        # 模拟盘总是成功
        status = OrderStatus.FILLED
        
        return OrderResponse(
            order_id=f"SIM_{uuid.uuid4().hex[:12]}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            status=status,
            message="Simulated"
        )
    
    async def _cancel_order_impl(self, order_id: str) -> bool:
        await asyncio.sleep(0.001)
        return True
    
    async def _query_orders_impl(self) -> List[OrderResponse]:
        return []
    
    async def _query_trades_impl(self) -> List[Trade]:
        return []
    
    async def _query_positions_impl(self) -> List[Position]:
        return []
    
    async def _query_account_impl(self) -> Account:
        return Account(
            account_id=self.config.account_id,
            total_assets=1000000.0,
            available_cash=500000.0,
            market_value=500000.0
        )


class BrokerManager:
    def __init__(self):
        self._brokers: Dict[str, BrokerAdapterBase] = {}
        self._default_broker: Optional[str] = None
        self._lock = asyncio.Lock()
    
    def register_broker(self, name: str, broker: BrokerAdapterBase, default: bool = False):
        self._brokers[name] = broker
        if default or self._default_broker is None:
            self._default_broker = name
    
    def get_broker(self, name: str = None) -> Optional[BrokerAdapterBase]:
        name = name or self._default_broker
        return self._brokers.get(name)
    
    async def connect_all(self) -> Dict[str, bool]:
        results = {}
        for name, broker in self._brokers.items():
            results[name] = await broker.connect()
        return results
    
    async def disconnect_all(self):
        for broker in self._brokers.values():
            await broker.disconnect()
    
    async def submit_order(self, request: OrderRequest, broker_name: str = None) -> OrderResponse:
        broker = self.get_broker(broker_name)
        if not broker:
            return OrderResponse(
                order_id="",
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="Broker not found"
            )
        return await broker.submit_order(request)
    
    async def cancel_order(self, order_id: str, broker_name: str = None) -> bool:
        broker = self.get_broker(broker_name)
        if not broker:
            return False
        return await broker.cancel_order(order_id)
    
    def list_brokers(self) -> List[str]:
        return list(self._brokers.keys())
    
    def get_connected_brokers(self) -> List[str]:
        return [name for name, broker in self._brokers.items() if broker.is_connected()]


# 全局实例
_broker_manager: Optional[BrokerManager] = None


def get_broker_manager() -> BrokerManager:
    global _broker_manager
    if _broker_manager is None:
        _broker_manager = BrokerManager()
        sim_config = BrokerConfig(
            broker_type=BrokerType.SIMULATOR,
            name="simulator",
            account_id="SIM_ACCOUNT",
        )
        _broker_manager.register_broker("simulator", SimulatorBroker(sim_config), default=True)
    return _broker_manager


def init_broker_manager() -> BrokerManager:
    global _broker_manager
    _broker_manager = BrokerManager()
    sim_config = BrokerConfig(
        broker_type=BrokerType.SIMULATOR,
        name="simulator",
        account_id="SIM_ACCOUNT",
    )
    _broker_manager.register_broker("simulator", SimulatorBroker(sim_config), default=True)
    return _broker_manager


# ════════════════════════════════════════════════════════════
# Enums and Dataclasses (defined at bottom for forward references)
# ═══════════════════════════════════════════════════════════

import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

logger = get_logger("execution.broker_adapter")


class BrokerType(Enum):
    CTP = "ctp"
    XTQUANT = "xtquant"
    HONGSU = "hongsu"
    ZHONGXIN = "zhongxin"
    GUOJIN = "guojin"
    SIMULATOR = "simulator"


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    FAK = "fak"
    FOK = "fok"


class OrderStatus(Enum):
    PENDING_NEW = "pending_new"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    PENDING_CANCEL = "pending_cancel"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TimeInForce(Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


@dataclass
class OrderRequest:
    symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0
    time_in_force: TimeInForce = TimeInForce.DAY
    account_id: str = ""
    strategy_id: str = ""
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4())[:16])
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResponse:
    order_id: str
    client_order_id: str
    symbol: str = ""
    status: OrderStatus = OrderStatus.PENDING_NEW
    message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Trade:
    trade_id: str
    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    price: float
    quantity: float
    timestamp: datetime
    commission: float = 0.0
    tax: float = 0.0


@dataclass
class Position:
    symbol: str
    long_quantity: float = 0.0
    short_quantity: float = 0.0
    long_avg_price: float = 0.0
    short_avg_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    update_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Account:
    account_id: str
    total_assets: float = 0.0
    available_cash: float = 0.0
    frozen_cash: float = 0.0
    market_value: float = 0.0
    margin_used: float = 0.0
    margin_available: float = 0.0
    update_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BrokerConfig:
    broker_type: BrokerType
    name: str
    api_key: str = ""
    api_secret: str = ""
    app_id: str = ""
    auth_code: str = ""
    front_address: str = ""
    account_id: str = ""
    password: str = ""
    max_orders_per_second: int = 100
    max_orders_per_minute: int = 3000
    reconnect_interval: int = 5
    max_reconnect_attempts: int = 10
    heartbeat_interval: int = 30

# ════════════════════════════════════════════════════════════
# 向后兼容别名 (供旧测试/代码使用)
# ═══════════════════════════════════════════════════════════

# 旧类名别名
BrokerAdapter = BrokerAdapterBase

class _SimulatedAdapterCompat(SimulatorBroker):
    def __init__(self, **kwargs):
        config = BrokerConfig(
            broker_type=BrokerType.SIMULATOR,
            name="simulated",
            account_id="SIM_ACCOUNT",
        )
        super().__init__(config)
        self._auto_connected = False
    
    def _ensure_connected(self):
        if not self._auto_connected:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            # 直接调用父类的 connect 避免递归
            loop.run_until_complete(super().connect())
            self._auto_connected = True
    
    # 同步包装器方法
    def connect(self):
        self._ensure_connected()
        return True
    
    def buy(self, symbol, price, shares, order_type="LIMIT"):
        self._ensure_connected()
        req = OrderRequest(symbol=symbol, side=OrderSide.BUY, quantity=shares, 
                          order_type=OrderType.LIMIT if order_type == "LIMIT" else OrderType.MARKET, price=price)
        loop = asyncio.get_event_loop()
        resp = loop.run_until_complete(self.submit_order(req))
        # 转换为旧格式 OrderResult
        return OrderResult(
            success=resp.status == OrderStatus.FILLED,
            symbol=resp.symbol or symbol,
            side="buy",
            shares=int(shares),
            price=price,
            filled_shares=int(shares) if resp.status == OrderStatus.FILLED else 0,
            filled_price=price,
            status=resp.status.value,
            error=resp.message if resp.status != OrderStatus.FILLED else "",
            is_simulated=True,
        )
    
    def sell(self, symbol, price, shares, order_type="MARKET"):
        self._ensure_connected()
        req = OrderRequest(symbol=symbol, side=OrderSide.SELL, quantity=shares,
                          order_type=OrderType.LIMIT if order_type == "LIMIT" else OrderType.MARKET, price=price)
        loop = asyncio.get_event_loop()
        resp = loop.run_until_complete(self.submit_order(req))
        # 转换为旧格式 OrderResult
        return OrderResult(
            success=resp.status == OrderStatus.FILLED,
            symbol=resp.symbol or symbol,
            side="sell",
            shares=int(shares),
            price=price,
            filled_shares=int(shares) if resp.status == OrderStatus.FILLED else 0,
            filled_price=price,
            status=resp.status.value,
            error=resp.message if resp.status != OrderStatus.FILLED else "",
            is_simulated=True,
        )
    
    def cancel(self, order_id):
        self._ensure_connected()
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.cancel_order(order_id))
    
    def get_positions(self):
        self._ensure_connected()
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.query_positions())
    
    def get_account(self):
        self._ensure_connected()
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.query_account())
    
    def get_orders(self, status=None):
        self._ensure_connected()
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.query_orders())
    
    def is_connected(self):
        self._ensure_connected()
        return True
    
    def disconnect(self):
        if self._auto_connected:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            loop.run_until_complete(super().disconnect())
            self._auto_connected = False

SimulatedAdapter = _SimulatedAdapterCompat

# 旧数据类别名
@dataclass
class OrderResult:
    success: bool
    symbol: str = ""
    side: str = ""
    shares: int = 0
    price: float = 0.0
    filled_shares: int = 0
    filled_price: float = 0.0
    status: str = ""
    error: str = ""
    is_simulated: bool = False
    quantity: int = 0  # 兼容字段
    
    def __post_init__(self):
        # 兼容旧字段
        if self.shares > 0 and self.quantity == 0:
            self.quantity = self.shares
    
    @property
    def is_filled(self):
        return self.status == "filled"

@dataclass
class AccountInfo:
    total_asset: float = 0.0
    available_cash: float = 0.0
    frozen_cash: float = 0.0
    positions: List[Dict] = field(default_factory=list)
    market_value: float = 0.0
    
    def __post_init__(self):
        if self.cash is not None and self.available_cash == 0:
            self.available_cash = self.cash


# 旧工厂函数别名
_adapter_instance: Optional[BrokerAdapterBase] = None

def get_broker_adapter(name: str = "simulated", **kwargs) -> BrokerAdapterBase:
    global _adapter_instance
    if _adapter_instance is None:
        if name == "simulated":
            config = BrokerConfig(
                broker_type=BrokerType.SIMULATOR,
                name="simulated",
                account_id=kwargs.get("db_path", "SIM_ACCOUNT"),
            )
            _adapter_instance = SimulatorBroker(config)
            asyncio.get_event_loop().run_until_complete(_adapter_instance.connect())
        else:
            raise ValueError(f"Unknown adapter: {name}")
    return _adapter_instance


def reset_adapter():
    global _adapter_instance
    if _adapter_instance:
        asyncio.get_event_loop().run_until_complete(_adapter_instance.disconnect())
    _adapter_instance = None


# 旧 VnpyAdapter 别名
def _cfg_get(key: str, default: str = "") -> str:
    """获取配置值 (兼容旧 API)."""
    from quant.config.constants import _require_cfg
    try:
        return _require_cfg(key)
    except Exception:
        return default


class VnpyAdapter(BrokerAdapterBase):
    name = "vnpy"
    _vnpy_available = False
    _VALID_ADAPTERS = ["simulated", "vnpy", "vnpy_ctp", "vnpy_xtp"]
    
    def __init__(self):
        super().__init__(BrokerConfig(broker_type=BrokerType.CTP, name="vnpy", account_id=""))
    
    def connect(self):
        """连接券商 (同步包装器，兼容旧 API)."""
        if not self._vnpy_available:
            raise RuntimeError("vnpy not installed")
        
        gateway_name = _cfg_get("vnpy.gateway", "CTP")
        if gateway_name not in self._VALID_ADAPTERS:
            raise ValueError(f"不在白名单内: {gateway_name}")
        
        # 实际连接逻辑
        pass
    
    def disconnect(self):
        pass
    
    def _on_trade(self, trade):
        """成交回调 - 非空实现."""
        self._emit("on_trade", trade)
    
    def _on_order(self, order):
        """订单回调 - 非空实现."""
        self._emit("on_order", order)
    
    def _on_position(self, position):
        """持仓回调 - 非空实现."""
        self._emit("on_position", position)
    
    async def _connect_impl(self) -> bool:
        if not self._vnpy_available:
            raise RuntimeError("vnpy not installed")
        return False
    
    async def _disconnect_impl(self):
        pass
    
    async def _submit_order_impl(self, request: OrderRequest) -> OrderResponse:
        return OrderResponse(
            order_id="",
            client_order_id=request.client_order_id,
            status=OrderStatus.REJECTED,
            message="vnpy not available"
        )
    
    async def _cancel_order_impl(self, order_id: str) -> bool:
        return False
    
    async def _query_orders_impl(self) -> List[OrderResponse]:
        return []
    
    async def _query_trades_impl(self) -> List[Trade]:
        return []
    
    async def _query_positions_impl(self) -> List[Position]:
        return []
    
    async def _query_account_impl(self) -> Account:
        return Account(account_id=self.config.account_id)


class VnpyCtpAdapter(VnpyAdapter):
    name = "vnpy_ctp"


class VnpyXtpAdapter(VnpyAdapter):
    name = "vnpy_xtp"


def _check_vnpy() -> bool:
    return False
