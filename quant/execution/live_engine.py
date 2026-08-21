"""实盘订单执行引擎 - 智能路由、分片执行、TWAP/VWAP/冰山单、成本模型校准."""

from __future__ import annotations
import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from quant.execution.broker_adapter import (
    BrokerAdapterBase, BrokerConfig, BrokerManager, BrokerType,
    OrderRequest, OrderResponse, OrderSide, OrderType, OrderStatus, TimeInForce,
    Trade, Position, Account, RateLimiter
)
from quant.execution.cost import CostModel
from quant.execution.execution_model import ExecutionContext, ExecutionResult, LiveExecutionModel
from quant.execution.engine import ExecutionEngine, Order
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

logger = get_logger("execution.live_engine")


class SliceAlgorithm(Enum):
    """分片算法."""
    TWAP = "twap"          # Time-Weighted Average Price
    VWAP = "vwap"          # Volume-Weighted Average Price
    ICEBERG = "iceberg"    # 冰山单 (显示小量, 隐藏大量)
    POV = "pov"            # Percentage of Volume
    IMPLEMENTATION_SHORTFALL = "is"  # Implementation Shortfall (到达价)


class OrderState(Enum):
    """订单状态."""
    PENDING = "pending"
    ROUTING = "routing"
    SLICING = "slicing"
    WORKING = "working"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class SliceConfig:
    """分片配置."""
    algorithm: SliceAlgorithm = SliceAlgorithm.TWAP
    total_shares: int = 0
    max_slice_size: int = 1000       # 单片最大股数
    min_slice_size: int = 100        # 单片最小股数
    max_slices: int = 20             # 最大分片数
    time_horizon_seconds: int = 3600 # 执行时间窗口 (秒)
    participation_rate: float = 0.1  # 参与率 (POV 算法用)
    price_limit: float = 0.0         # 价格限制 (0=不限制)
    display_size: int = 0            # 冰山单显示量 (0=自动)
    randomize_slices: bool = True    # 随机化分片大小/时间
    urgency: float = 0.5             # 紧迫度 0-1 (IS 算法用)


@dataclass
class OrderSlice:
    """单个分片."""
    slice_id: str
    parent_order_id: str
    symbol: str
    side: OrderSide
    shares: int
    price: float
    order_type: OrderType
    time_in_force: TimeInForce
    status: OrderState = OrderState.PENDING
    filled_shares: int = 0
    avg_fill_price: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    broker_order_id: str = ""
    metadata: Dict = field(default_factory=dict)


@dataclass
class ParentOrder:
    """母单."""
    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    total_shares: int
    target_price: float
    order_type: OrderType
    slice_config: SliceConfig
    state: OrderState = OrderState.PENDING
    slices: List[OrderSlice] = field(default_factory=list)
    filled_shares: int = 0
    avg_fill_price: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    strategy_id: str = ""
    account_id: str = ""
    metadata: Dict = field(default_factory=dict)


class SliceAlgorithmBase(ABC):
    """分片算法基类."""

    def __init__(self, config: SliceConfig):
        self.config = config

    @abstractmethod
    def generate_slices(self, parent: ParentOrder, market_data: Dict) -> List[OrderSlice]:
        """生成分片计划."""
        pass

    @abstractmethod
    def on_slice_fill(self, parent: ParentOrder, slice_: OrderSlice, fill: Trade) -> List[OrderSlice]:
        """分片成交后的回调，可能生成新分片."""
        pass

    @abstractmethod
    def on_market_update(self, parent: ParentOrder, market_data: Dict) -> List[OrderSlice]:
        """行情更新回调，可能调整分片."""
        pass


class TWAPAlgorithm(SliceAlgorithmBase):
    """TWAP (Time-Weighted Average Price) - 均匀时间分片."""

    def generate_slices(self, parent: ParentOrder, market_data: Dict) -> List[OrderSlice]:
        slices = []
        n_slices = min(self.config.max_slices, 
                       max(1, self.config.total_shares // self.config.max_slice_size))
        
        # 计算时间间隔
        interval = self.config.time_horizon_seconds / n_slices
        base_size = self.config.total_shares // n_slices
        remainder = self.config.total_shares % n_slices
        
        for i in range(n_slices):
            # 随机化大小 (±10%)
            size = base_size + (1 if i < remainder else 0)
            if self.config.randomize_slices:
                size = int(size * np.random.uniform(0.9, 1.1))
                size = max(self.config.min_slice_size, min(size, self.config.max_slice_size))
            
            # 计算预期执行时间
            expected_time = parent.created_at.timestamp() + i * interval
            if self.config.randomize_slices:
                expected_time += np.random.uniform(-interval * 0.1, interval * 0.1)
            
            slice_ = OrderSlice(
                slice_id=f"{parent.order_id}_s{i}",
                parent_order_id=parent.order_id,
                symbol=parent.symbol,
                side=parent.side,
                shares=size,
                price=parent.target_price,
                order_type=parent.order_type,
                time_in_force=TimeInForce.DAY,
                metadata={
                    "algorithm": "TWAP",
                    "slice_index": i,
                    "total_slices": n_slices,
                    "expected_time": expected_time,
                }
            )
            slices.append(slice_)
        
        return slices

    def on_slice_fill(self, parent: ParentOrder, slice_: OrderSlice, fill: Trade) -> List[OrderSlice]:
        # TWAP 不根据成交动态调整
        return []

    def on_market_update(self, parent: ParentOrder, market_data: Dict) -> List[OrderSlice]:
        return []


class VWAPAlgorithm(SliceAlgorithmBase):
    """VWAP (Volume-Weighted Average Price) - 按成交量分布分片."""

    def generate_slices(self, parent: ParentOrder, market_data: Dict) -> List[OrderSlice]:
        slices = []
        # 获取历史成交量分布 (需要从行情获取)
        volume_profile = market_data.get("volume_profile", {})
        if not volume_profile:
            # 无成交量分布时退化为 TWAP
            return TWAPAlgorithm(self.config).generate_slices(parent, market_data)
        
        # 按成交量分布分配
        total_volume = sum(volume_profile.values())
        slices = []
        remaining = parent.total_shares
        
        for i, (time_bucket, vol_pct) in enumerate(sorted(volume_profile.items())):
            if i >= self.config.max_slices:
                break
            size = int(parent.total_shares * vol_pct / total_volume)
            if size < self.config.min_slice_size:
                continue
            size = min(size, self.config.max_slice_size, remaining)
            if size <= 0:
                continue
            
            slice_ = OrderSlice(
                slice_id=f"{parent.order_id}_s{i}",
                parent_order_id=parent.order_id,
                symbol=parent.symbol,
                side=parent.side,
                shares=size,
                price=parent.target_price,
                order_type=parent.order_type,
                time_in_force=TimeInForce.DAY,
                metadata={
                    "algorithm": "VWAP",
                    "time_bucket": time_bucket,
                    "volume_pct": vol_pct,
                }
            )
            slices.append(slice_)
            remaining -= size
        
        return slices

    def on_slice_fill(self, parent: ParentOrder, slice_: OrderSlice, fill: Trade) -> List[OrderSlice]:
        return []

    def on_market_update(self, parent: ParentOrder, market_data: Dict) -> List[OrderSlice]:
        return []


class IcebergAlgorithm(SliceAlgorithmBase):
    """Iceberg (冰山单) - 显示小量，隐藏大量."""

    def generate_slices(self, parent: ParentOrder, market_data: Dict) -> List[OrderSlice]:
        slices = []
        display = self.config.display_size or max(100, self.config.max_slice_size // 5)
        remaining = parent.total_shares
        i = 0
        
        while remaining > 0 and i < self.config.max_slices:
            size = min(display, remaining, self.config.max_slice_size)
            if size < self.config.min_slice_size:
                size = min(remaining, self.config.min_slice_size)
            
            slice_ = OrderSlice(
                slice_id=f"{parent.order_id}_s{i}",
                parent_order_id=parent.order_id,
                symbol=parent.symbol,
                side=parent.side,
                shares=size,
                price=parent.target_price,
                order_type=parent.order_type,
                time_in_force=TimeInForce.DAY,
                metadata={
                    "algorithm": "ICEBERG",
                    "display_size": display,
                    "hidden": remaining - size,
                    "slice_index": i,
                }
            )
            slices.append(slice_)
            remaining -= size
            i += 1
        
        return slices

    def on_slice_fill(self, parent: ParentOrder, slice_: OrderSlice, fill: Trade) -> List[OrderSlice]:
        # 冰山单：当前片成交后自动发布下一片
        remaining_slices = [s for s in parent.slices if s.status == OrderState.PENDING]
        if remaining_slices:
            next_slice = remaining_slices[0]
            return [next_slice]  # 触发发布下一片
        return []

    def on_market_update(self, parent: ParentOrder, market_data: Dict) -> List[OrderSlice]:
        return []


class POVAlgorithm(SliceAlgorithmBase):
    """POV (Percentage of Volume) - 按市场成交量比例参与."""

    def generate_slices(self, parent: ParentOrder, market_data: Dict) -> List[OrderSlice]:
        slices = []
        # 预估市场成交量
        est_market_volume = market_data.get("estimated_volume", parent.total_shares * 10)
        target_volume = int(est_market_volume * self.config.participation_rate)
        target_volume = min(target_volume, parent.total_shares)
        
        if target_volume < self.config.min_slice_size:
            target_volume = min(self.config.min_slice_size, parent.total_shares)
        
        n_slices = min(self.config.max_slices, 
                       max(1, target_volume // self.config.max_slice_size))
        
        base_size = target_volume // n_slices
        remainder = target_volume % n_slices
        
        for i in range(n_slices):
            size = base_size + (1 if i < remainder else 0)
            if self.config.randomize_slices:
                size = int(size * np.random.uniform(0.9, 1.1))
                size = max(self.config.min_slice_size, min(size, self.config.max_slice_size))
            
            slice_ = OrderSlice(
                slice_id=f"{parent.order_id}_s{i}",
                parent_order_id=parent.order_id,
                symbol=parent.symbol,
                side=parent.side,
                shares=size,
                price=parent.target_price,
                order_type=parent.order_type,
                time_in_force=TimeInForce.DAY,
                metadata={
                    "algorithm": "POV",
                    "participation_rate": self.config.participation_rate,
                    "slice_index": i,
                    "total_slices": n_slices,
                }
            )
            slices.append(slice_)
        
        return slices

    def on_market_update(self, parent: ParentOrder, market_data: Dict) -> List[OrderSlice]:
        # 实时成交量更新时动态调整
        current_volume = market_data.get("current_volume", 0)
        target_volume = int(current_volume * self.config.participation_rate)
        # 这里可以动态调整后续分片大小
        return []


class ImplementationShortfallAlgorithm(SliceAlgorithmBase):
    """Implementation Shortfall (IS) - 到达价算法，平衡市场冲击和机会成本."""

    def generate_slices(self, parent: ParentOrder, market_data: Dict) -> List[OrderSlice]:
        slices = []
        # IS 算法核心：根据紧迫度和波动率计算最优轨迹
        volatility = market_data.get("volatility", 0.02)  # 日波动率
        urgency = self.config.urgency
        
        # 根据 Almgren-Chriss 模型计算最优执行轨迹
        # 简化版：紧迫度高 -> 前置执行；紧迫度低 -> 后置执行
        n_slices = min(self.config.max_slices, 
                       max(5, self.config.total_shares // self.config.max_slice_size))
        
        # 计算时间衰减因子
        if urgency > 0.7:
            # 高紧迫：指数衰减，前重后轻
            weights = np.exp(-np.linspace(0, 3, n_slices))
        elif urgency > 0.3:
            # 中等：线性衰减
            weights = np.linspace(1.5, 0.5, n_slices)
        else:
            # 低紧迫：后置执行
            weights = np.exp(np.linspace(-3, 0, n_slices))
        
        weights = weights / weights.sum()
        shares_per_slice = (weights * parent.total_shares).astype(int)
        # 修正总数
        diff = parent.total_shares - shares_per_slice.sum()
        shares_per_slice[0] += diff
        
        slices = []
        for i, size in enumerate(shares_per_slice):
            if size < self.config.min_slice_size:
                continue
            size = min(size, self.config.max_slice_size)
            
            slice_ = OrderSlice(
                slice_id=f"{parent.order_id}_s{i}",
                parent_order_id=parent.order_id,
                symbol=parent.symbol,
                side=parent.side,
                shares=size,
                price=parent.target_price,
                order_type=parent.order_type,
                time_in_force=TimeInForce.DAY,
                metadata={
                    "algorithm": "IS",
                    "urgency": urgency,
                    "volatility": volatility,
                    "weight": float(weights[i]),
                    "slice_index": i,
                }
            )
            slices.append(slice_)
        
        return slices

    def on_market_update(self, parent: ParentOrder, market_data: Dict) -> List[OrderSlice]:
        # IS 算法根据价格偏离动态调整
        # 价格不利移动时加速执行
        return []


class SmartRouter:
    """智能路由器 - 根据订单特征选择最优券商和算法."""

    def __init__(self, broker_manager: BrokerManager):
        self.broker_manager = broker_manager
        self._algorithm_map = {
            SliceAlgorithm.TWAP: TWAPAlgorithm,
            SliceAlgorithm.VWAP: VWAPAlgorithm,
            SliceAlgorithm.ICEBERG: IcebergAlgorithm,
            SliceAlgorithm.POV: POVAlgorithm,
            SliceAlgorithm.IMPLEMENTATION_SHORTFALL: ImplementationShortfallAlgorithm,
        }

    def select_broker(self, order: ParentOrder) -> BrokerAdapterBase:
        """选择最优券商."""
        connected = self.broker_manager.get_connected_brokers()
        if not connected:
            raise RuntimeError("No connected brokers available")
        
        # 简单策略：优先使用模拟盘 (测试)，生产环境按手续费/速度路由
        if "simulator" in connected:
            return self.broker_manager.get_broker("simulator")
        return self.broker_manager.get_broker(connected[0])

    def select_algorithm(self, order: ParentOrder, market_data: Dict) -> SliceAlgorithmBase:
        """根据订单特征自动选择算法."""
        algo = order.slice_config.algorithm
        if algo in self._algorithm_map:
            return self._algorithm_map[algo](order.slice_config)
        
        # 自动选择逻辑
        if order.total_shares > 50000:
            return VWAPAlgorithm(order.slice_config)  # 大单用 VWAP
        elif order.slice_config.urgency > 0.7:
            return ImplementationShortfallAlgorithm(order.slice_config)  # 紧急单用 IS
        else:
            return TWAPAlgorithm(order.slice_config)  # 默认 TWAP


class LiveOrderExecutionEngine:
    """实盘订单执行引擎 - 统一入口."""

    def __init__(
        self,
        broker_manager: BrokerManager,
        cost_model: CostModel = None,
        execution_model: LiveExecutionModel = None,
    ):
        self.broker_manager = broker_manager
        self.cost_model = cost_model or CostModel.from_config()
        self.execution_model = execution_model or LiveExecutionModel()
        self.router = SmartRouter(broker_manager)
        self._active_orders: Dict[str, ParentOrder] = {}
        self._slice_tasks: Dict[str, asyncio.Task] = {}
        self._running = False
        self._lock = asyncio.Lock()
        
        # 回调
        self.on_order_update: Optional[Callable[[ParentOrder], None]] = None
        self.on_slice_fill: Optional[Callable[[ParentOrder, OrderSlice, Trade], None]] = None
        self.on_order_complete: Optional[Callable[[ParentOrder], None]] = None
        self.on_error: Optional[Callable[[str, Exception], None]] = None

    async def submit_order(self, order: ParentOrder) -> str:
        """提交母单."""
        async with self._lock:
            if order.order_id in self._active_orders:
                raise ValueError(f"Order {order.order_id} already exists")
            
            order.state = OrderState.ROUTING
            self._active_orders[order.order_id] = order
            
            # 选择券商
            broker = self.router.select_broker(order)
            order.metadata["broker"] = broker.config.name
            
            # 选择算法
            algorithm = self.router.select_algorithm(order, {})
            
            # 生成分片计划
            slices = algorithm.generate_slices(order, {})
            order.slices = slices
            
            order.state = OrderState.SLICING
            self._notify_order_update(order)
            
            # 启动执行任务
            task = asyncio.create_task(self._execute_order(order, algorithm))
            self._slice_tasks[order.order_id] = task
            
            logger.info(f"Submitted parent order {order.order_id} with {len(slices)} slices")
            return order.order_id

    async def _execute_order(self, parent: ParentOrder, algorithm: SliceAlgorithmBase):
        """执行母单分片."""
        try:
            parent.state = OrderState.WORKING
            self._notify_order_update(parent)
            
            broker = self.broker_manager.get_broker(parent.metadata.get("broker"))
            
            # 发布分片 (根据算法调度)
            if parent.slice_config.algorithm == SliceAlgorithm.ICEBERG:
                await self._execute_iceberg(parent, algorithm, broker)
            else:
                await self._execute_parallel(parent, algorithm, broker)
            
            # 等待完成
            await self._wait_completion(parent)
            
        except Exception as e:
            logger.error(f"Order {parent.order_id} execution failed: {e}")
            parent.state = OrderState.REJECTED
            parent.metadata["error"] = str(e)
            self._notify_order_update(parent)
            if self.on_error:
                self.on_error(parent.order_id, e)
        finally:
            async with self._lock:
                self._slice_tasks.pop(parent.order_id, None)

    async def _execute_iceberg(self, parent: ParentOrder, algorithm: IcebergAlgorithm, broker: BrokerAdapterBase):
        """冰山单：串行发布，前一片成交后发布下一片."""
        pending = [s for s in parent.slices if s.status == OrderState.PENDING]
        
        for slice_ in parent.slices:
            slice_.state = OrderState.ROUTING
            self._notify_order_update(parent)
            
            # 提交分片
            req = OrderRequest(
                symbol=parent.symbol,
                side=parent.side,
                quantity=slice_.shares,
                order_type=slice_.order_type,
                price=slice_.price,
                time_in_force=slice_.time_in_force,
                account_id=parent.account_id,
                strategy_id=parent.strategy_id,
                client_order_id=slice_.slice_id,
            )
            
            resp = await broker.submit_order(req)
            slice_.broker_order_id = resp.order_id
            slice_.state = OrderState.WORKING
            self._notify_order_update(parent)
            
            # 等待成交
            await self._wait_slice_fill(slice_, broker)
            
            if parent.state == OrderState.CANCELLED:
                break

    async def _execute_parallel(self, parent: ParentOrder, algorithm: SliceAlgorithmBase, broker: BrokerAdapterBase):
        """并行发布分片 (TWAP/VWAP/POV/IS)."""
        # 限制并发数
        semaphore = asyncio.Semaphore(5)
        
        async def submit_slice(slice_: OrderSlice):
            async with semaphore:
                if parent.state == OrderState.CANCELLED:
                    return
                
                slice_.state = OrderState.ROUTING
                self._notify_order_update(parent)
                
                req = OrderRequest(
                    symbol=parent.symbol,
                    side=parent.side,
                    quantity=slice_.shares,
                    order_type=slice_.order_type,
                    price=slice_.price,
                    time_in_force=slice_.time_in_force,
                    account_id=parent.account_id,
                    strategy_id=parent.strategy_id,
                    client_order_id=slice_.slice_id,
                )
                
                resp = await broker.submit_order(req)
                slice_.broker_order_id = resp.order_id
                slice_.state = OrderState.WORKING
                self._notify_order_update(parent)
                
                # 等待成交
                await self._wait_slice_fill(slice_, broker)
        
        tasks = [submit_slice(s) for s in parent.slices if s.status == OrderState.PENDING]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait_slice_fill(self, slice_: OrderSlice, broker: BrokerAdapterBase):
        """等待分片成交."""
        timeout = 300  # 5分钟超时
        start = time.time()
        
        while slice_.state == OrderState.WORKING:
            if time.time() - start > timeout:
                slice_.state = OrderState.EXPIRED
                logger.warning(f"Slice {slice_.slice_id} expired")
                break
            
            # 查询订单状态
            orders = await broker.query_orders()
            for o in orders:
                if o.client_order_id == slice_.slice_id:
                    if o.status == OrderStatus.FILLED:
                        slice_.state = OrderState.FILLED
                        slice_.filled_shares = slice_.shares
                        slice_.avg_fill_price = o.price if hasattr(o, 'price') else slice_.price
                        # 获取成交回报
                        trades = await broker.query_trades()
                        for t in trades:
                            if t.order_id == slice_.broker_order_id:
                                self._on_slice_filled(slice_, t)
                    elif o.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                        slice_.state = OrderState.CANCELLED if o.status == OrderStatus.CANCELLED else OrderState.REJECTED
                    break
            
            await asyncio.sleep(1)

    def _on_slice_filled(self, slice_: OrderSlice, trade: Trade):
        """分片成交回调."""
        slice_.filled_shares = trade.quantity
        slice_.avg_fill_price = trade.price
        slice_.updated_at = datetime.utcnow()
        
        # 更新母单
        parent = self._active_orders.get(slice_.parent_order_id)
        if parent:
            parent.filled_shares += trade.quantity
            # 加权平均价
            if parent.avg_fill_price == 0:
                parent.avg_fill_price = trade.price
            else:
                total = parent.filled_shares
                prev = total - trade.quantity
                parent.avg_fill_price = (parent.avg_fill_price * prev + trade.price * trade.quantity) / total
            parent.updated_at = datetime.utcnow()
            
            if self.on_slice_fill:
                parent_obj = self._active_orders.get(slice_.parent_order_id)
                if parent_obj:
                    self.on_slice_fill(parent_obj, slice_, trade)

    async def _wait_completion(self, parent: ParentOrder):
        """等待母单完成."""
        while parent.state == OrderState.WORKING:
            filled = sum(s.filled_shares for s in parent.slices)
            if filled >= parent.total_shares:
                parent.state = OrderState.FILLED
                parent.completed_at = datetime.utcnow()
                break
            
            failed = sum(1 for s in parent.slices if s.state in (OrderState.REJECTED, OrderState.EXPIRED, OrderState.CANCELLED))
            if failed > len(parent.slices) / 2:
                parent.state = OrderState.REJECTED
                break
            
            await asyncio.sleep(1)
        
        parent.completed_at = datetime.utcnow() or datetime.utcnow()
        self._notify_order_update(parent)
        
        if self.on_order_complete:
            self.on_order_complete(parent)

    def cancel_order(self, order_id: str) -> bool:
        """取消母单."""
        parent = self._active_orders.get(order_id)
        if not parent:
            return False
        
        parent.state = OrderState.CANCELLED
        # 取消所有未成交分片
        for slice_ in parent.slices:
            if slice_.state in (OrderState.PENDING, OrderState.ROUTING, OrderState.WORKING):
                slice_.state = OrderState.CANCELLED
        self._notify_order_update(parent)
        return True

    def get_order(self, order_id: str) -> Optional[ParentOrder]:
        return self._active_orders.get(order_id)

    def get_active_orders(self) -> List[ParentOrder]:
        return [o for o in self._active_orders.values() if o.state in (OrderState.WORKING, OrderState.SLICING, OrderState.ROUTING)]

    def _notify_order_update(self, order: ParentOrder):
        if self.on_order_update:
            self.on_order_update(order)


class CostModelCalibrator:
    """成本模型校准器 - 基于历史成交数据校准滑点/冲击参数."""

    def __init__(self, cost_model: CostModel):
        self.cost_model = cost_model
        self._calibration_data: List[Dict] = []

    def record_execution(self, order: ParentOrder):
        """记录执行数据用于校准."""
        for slice_ in order.slices:
            if slice_.state == OrderState.FILLED and slice_.filled_shares > 0:
                self._calibration_data.append({
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "shares": slice_.filled_shares,
                    "target_price": order.target_price,
                    "fill_price": slice_.avg_fill_price,
                    "timestamp": slice_.updated_at,
                    "algorithm": order.slice_config.algorithm.value,
                    "slice_size": slice_.shares,
                })

    def calibrate_slippage(self, lookback_days: int = 30) -> Dict:
        """校准滑点参数."""
        if not self._calibration_data:
            return {"status": "no_data"}
        
        cutoff = datetime.utcnow().timestamp() - lookback_days * 86400
        recent = [d for d in self._calibration_data if d["timestamp"].timestamp() > cutoff]
        
        if not recent:
            return {"status": "no_recent_data"}
        
        df = pd.DataFrame(recent)
        
        # 计算实际滑点 (bp)
        df["slippage_bps"] = 0.0
        buy_mask = df["side"] == "buy"
        sell_mask = df["side"] == "sell"
        
        df.loc[buy_mask, "slippage_bps"] = (df.loc[buy_mask, "fill_price"] - df.loc[buy_mask, "target_price"]) / df.loc[buy_mask, "target_price"] * 10000
        df.loc[sell_mask, "slippage_bps"] = (df.loc[sell_mask, "target_price"] - df.loc[sell_mask, "fill_price"]) / df.loc[sell_mask, "target_price"] * 10000
        
        # 按算法/股票/方向分组统计
        results = {}
        for (algo, symbol, side), group in df.groupby(["algorithm", "symbol", "side"]):
            if len(group) < 3:
                continue
            results[f"{algo}_{symbol}_{side}"] = {
                "count": len(group),
                "mean_slippage_bps": group["slippage_bps"].mean(),
                "std_slippage_bps": group["slippage_bps"].std(),
                "median_slippage_bps": group["slippage_bps"].median(),
                "p95_slippage_bps": group["slippage_bps"].quantile(0.95),
            }
        
        return {
            "total_samples": len(recent),
            "by_group": results,
            "overall_mean": df["slippage_bps"].mean(),
            "overall_median": df["slippage_bps"].median(),
        }

    def calibrate_impact(self) -> Dict:
        """校准市场冲击参数 (eta)."""
        if not self._calibration_data:
            return {"status": "no_data"}
        
        df = pd.DataFrame(self._calibration_data)
        
        # 使用 Almgren-Chriss 模型拟合 eta
        # slippage = eta * sqrt(shares * price / daily_volume)
        # 简化：假设 daily_volume 固定，拟合 eta
        pass


# 全局实例
_live_engine: Optional[LiveOrderExecutionEngine] = None
_cost_calibrator: Optional[CostModelCalibrator] = None


def get_live_engine() -> LiveOrderExecutionEngine:
    global _live_engine
    if _live_engine is None:
        from quant.execution.broker_adapter import get_broker_manager
        _live_engine = LiveOrderExecutionEngine(
            broker_manager=get_broker_manager(),
            cost_model=CostModel.from_config(),
            execution_model=LiveExecutionModel(),
        )
    return _live_engine


def init_live_engine(
    broker_manager: BrokerManager = None,
    cost_model: CostModel = None,
    execution_model: LiveExecutionModel = None,
) -> LiveOrderExecutionEngine:
    global _live_engine
    from quant.execution.broker_adapter import get_broker_manager
    _live_engine = LiveOrderExecutionEngine(
        broker_manager=broker_manager or get_broker_manager(),
        cost_model=cost_model or CostModel.from_config(),
        execution_model=execution_model or LiveExecutionModel(),
    )
    return _live_engine


def get_cost_calibrator() -> CostModelCalibrator:
    global _cost_calibrator
    if _cost_calibrator is None:
        _cost_calibrator = CostModelCalibrator(CostModel.from_config())
    return _cost_calibrator
