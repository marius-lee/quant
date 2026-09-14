"""券商类型枚举 + 数据类.

从 broker_adapter.py 提取（第一份，保留 docstring）。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict
import uuid


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
    quantity: float
    avg_cost: float = 0.0
    market_value: float = 0.0
    pnl: float = 0.0
    today_quantity: float = 0.0
    today_pnl: float = 0.0
    frozen_quantity: float = 0.0


@dataclass
class Account:
    """账户."""
    account_id: str
    total_asset: float = 0.0
    available: float = 0.0
    frozen: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)


@dataclass
class BrokerConfig:
    """券商配置."""
    name: str
    broker_type: BrokerType = BrokerType.SIMULATOR
    enabled: bool = True
    config: Dict[str, Any] = field(default_factory=dict)


class RateLimiter:
    """简单速率限制器."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._calls: list = []

    def acquire(self) -> bool:
        now = time.time()
        self._calls = [t for t in self._calls if now - t < self.period]
        if len(self._calls) < self.max_calls:
            self._calls.append(now)
            return True
        return False


import time


__all__ = [
    'BrokerType', 'OrderSide', 'OrderType', 'OrderStatus', 'TimeInForce',
    'OrderRequest', 'OrderResponse', 'Trade', 'Position', 'Account',
    'BrokerConfig', 'RateLimiter',
]