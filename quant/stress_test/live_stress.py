"""全链路实盘压测框架 - 模拟实盘流量、并发下单、行情订阅、持仓同步、风控计算."""

from __future__ import annotations
import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from collections import defaultdict, deque

import numpy as np

from quant.execution.broker_adapter import (
    BrokerAdapterBase, BrokerManager, BrokerType, OrderRequest, OrderResponse,
    OrderSide, OrderType, OrderStatus, Trade, Position, Account
)
from quant.execution.live_engine import (
    LiveOrderExecutionEngine, ParentOrder, OrderSlice, OrderState,
    SliceAlgorithm, SliceConfig
)
from quant.risk.live_risk import LiveRiskManager, get_live_risk_manager
from quant.execution.engine import ExecutionEngine
from quant.execution.cost import CostModel
from quant.execution.execution_model import ExecutionContext, LiveExecutionModel
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

logger = get_logger("stress_test.live")


class StressTestMode(Enum):
    ORDER_ONLY = "order_only"
    FULL_CHAIN = "full_chain"
    MARKET_DATA = "market_data"
    POSITION_SYNC = "position_sync"
    RISK_CALC = "risk_calc"
    MIXED = "mixed"


class StressTestPhase(Enum):
    WARMUP = "warmup"
    RAMP_UP = "ramp_up"
    STEADY = "steady"
    SPIKE = "spike"
    COOLDOWN = "cooldown"


@dataclass
class StressTestConfig:
    mode: StressTestMode = StressTestMode.FULL_CHAIN
    duration_seconds: int = 300
    target_qps: int = 10000
    ramp_up_seconds: int = 60
    steady_seconds: int = 180
    spike_qps: int = 20000
    spike_duration: int = 30
    cooldown_seconds: int = 30
    max_concurrent_orders: int = 1000
    max_concurrent_market_subs: int = 500
    symbols: List[str] = field(default_factory=lambda: [f"SH{str(i).zfill(6)}" for i in range(1, 101)])
    order_sides: List[str] = field(default_factory=lambda: ["buy", "sell"])
    order_types: List[str] = field(default_factory=lambda: ["limit", "market"])
    price_range: Tuple[float, float] = (5.0, 100.0)
    quantity_range: Tuple[int, int] = (100, 10000)
    strategy_count: int = 10
    account_count: int = 5
    tick_frequency_ms: int = 100
    symbols_per_sub: int = 50
    position_sync_interval_ms: int = 1000
    risk_calc_interval_ms: int = 100
    risk_rules_count: int = 20


@dataclass
class StressTestMetrics:
    orders_sent: int = 0
    orders_acked: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    orders_failed: int = 0
    order_latency_p50: float = 0.0
    order_latency_p95: float = 0.0
    order_latency_p99: float = 0.0
    order_latency_max: float = 0.0
    market_ticks_received: int = 0
    market_latency_p50: float = 0.0
    market_latency_p99: float = 0.0
    position_syncs: int = 0
    position_sync_latency_p99: float = 0.0
    position_deltas: int = 0
    risk_calcs: int = 0
    risk_calc_latency_p99: float = 0.0
    risk_blocks: int = 0
    cpu_usage_pct: float = 0.0
    memory_usage_mb: float = 0.0
    network_io_mbps: float = 0.0
    errors_by_type: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    connection_errors: int = 0
    timeout_errors: int = 0

    def to_dict(self) -> Dict:
        return {
            "orders": {
                "sent": self.orders_sent,
                "acked": self.orders_acked,
                "filled": self.orders_filled,
                "rejected": self.orders_rejected,
                "failed": self.orders_failed,
                "success_rate": self.orders_acked / max(self.orders_sent, 1),
            },
            "latency_ms": {
                "p50": self.order_latency_p50,
                "p95": self.order_latency_p95,
                "p99": self.order_latency_p99,
                "max": self.order_latency_max,
            },
            "market_data": {
                "ticks_received": self.market_ticks_received,
                "latency_p50": self.market_latency_p50,
                "latency_p99": self.market_latency_p99,
            },
            "position_sync": {
                "count": self.position_syncs,
                "latency_p99": self.position_sync_latency_p99,
                "deltas": self.position_deltas,
            },
            "risk_control": {
                "calculations": self.risk_calcs,
                "latency_p99": self.risk_calc_latency_p99,
                "blocks": self.risk_blocks,
            },
            "system": {
                "cpu_pct": self.cpu_usage_pct,
                "memory_mb": self.memory_usage_mb,
                "network_mbps": self.network_io_mbps,
            },
            "errors": {
                "by_type": dict(self.errors_by_type),
                "connection": self.connection_errors,
                "timeout": self.timeout_errors,
            },
        }


class LatencyTracker:
    def __init__(self, max_samples: int = 100000):
        self._samples: List[float] = []
        self._max_samples = max_samples
        self._lock = asyncio.Lock()

    def record(self, latency_ms: float):
        async def _record():
            async with self._lock:
                if len(self._samples) >= self._max_samples:
                    idx = random.randint(0, len(self._samples))
                    if idx < self._max_samples:
                        self._samples[idx] = latency_ms
                else:
                    self._samples.append(latency_ms)
        asyncio.create_task(_record())

    def get_percentiles(self) -> Dict[str, float]:
        if not self._samples:
            return {"p50": 0, "p95": 0, "p99": 0, "max": 0}
        sorted_samples = sorted(self._samples)
        n = len(sorted_samples)
        return {
            "p50": sorted_samples[int(n * 0.50)],
            "p95": sorted_samples[int(n * 0.95)],
            "p99": sorted_samples[int(n * 0.99)],
            "max": sorted_samples[-1],
        }


class MockBroker(BrokerAdapterBase):
    def __init__(
        self,
        config: BrokerConfig,
        base_latency_ms: float = 5.0,
        latency_jitter_ms: float = 2.0,
        failure_rate: float = 0.0,
        reject_rate: float = 0.0,
    ):
        super().__init__(config)
        self._base_latency = base_latency_ms
        self._latency_jitter = latency_jitter_ms
        self._failure_rate = failure_rate
        self._reject_rate = reject_rate
        self._connected = False
        self._order_store: Dict[str, OrderResponse] = {}
        self._trade_store: Dict[str, Trade] = {}
        self._position_store: Dict[str, Position] = {}
        self._account = Account(
            account_id=config.account_id,
            total_assets=10000000.0,
            available_cash=5000000.0,
            frozen_cash=1000000.0,
            margin_ratio=3.0,
        )

    async def _connect_impl(self) -> bool:
        await asyncio.sleep(0.01)
        self._connected = True
        return True

    async def _disconnect_impl(self):
        self._connected = False

    async def _submit_order_impl(self, request: OrderRequest) -> OrderResponse:
        latency = max(0, random.gauss(self._base_latency, self._latency_jitter))
        await asyncio.sleep(latency / 1000)

        if random.random() < self._failure_rate:
            return OrderResponse(
                order_id="",
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="Simulated connection failure",
            )

        if random.random() < self._reject_rate:
            return OrderResponse(
                order_id=f"REJ_{uuid.uuid4().hex[:8]}",
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="Simulated reject",
            )

        order_id = f"ORD_{uuid.uuid4().hex[:12]}"
        self._order_store[request.client_order_id] = OrderResponse(
            order_id=order_id,
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            status=OrderStatus.SUBMITTED,
        )

        asyncio.create_task(self._simulate_fill(request.client_order_id, request))

        return OrderResponse(
            order_id=order_id,
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            status=OrderStatus.SUBMITTED,
        )

    async def _simulate_fill(self, client_order_id: str, request: OrderRequest):
        fill_delay = random.uniform(0.05, 0.5)
        await asyncio.sleep(fill_delay)

        if client_order_id in self._order_store:
            order = self._order_store[client_order_id]
            order.status = OrderStatus.FILLED

            trade = Trade(
                trade_id=f"TRD_{uuid.uuid4().hex[:12]}",
                order_id=order.order_id,
                client_order_id=client_order_id,
                symbol=request.symbol,
                side=request.side,
                price=request.price if request.order_type == OrderType.LIMIT else request.price * random.uniform(0.999, 1.001),
                quantity=request.quantity,
                timestamp=datetime.utcnow(),
                commission=request.target_price * request.quantity * 0.0003,
                tax=request.target_price * request.quantity * 0.001 if request.side == OrderSide.SELL else 0,
            )
            self._trade_store[trade.trade_id] = trade

            if request.side == OrderSide.BUY:
                pos = self._position_store.get(request.symbol)
                if not pos:
                    pos = Position(
                        symbol=request.symbol,
                        long_quantity=0,
                        long_avg_price=0,
                    )
                    self._position_store[request.symbol] = pos
                pos.long_quantity += request.quantity
                pos.long_avg_price = (pos.long_avg_price * (pos.long_quantity - request.quantity) + request.price * request.quantity) / pos.long_quantity
            else:
                pos = self._position_store.get(request.symbol)
                if pos:
                    pos.long_quantity -= request.quantity

    async def _cancel_order_impl(self, order_id: str) -> bool:
        await asyncio.sleep(0.005)
        return True

    async def _query_orders_impl(self) -> List[OrderResponse]:
        await asyncio.sleep(0.002)
        return list(self._order_store.values())

    async def _query_trades_impl(self) -> List[Trade]:
        await asyncio.sleep(0.002)
        return list(self._trade_store.values())

    async def _query_positions_impl(self) -> List[Position]:
        await asyncio.sleep(0.002)
        return list(self._position_store.values())

    async def _query_account_impl(self) -> Account:
        await asyncio.sleep(0.001)
        return self._account


class StressTestRunner:
    def __init__(self, config: StressTestConfig):
        self.config = config
        self.metrics = StressTestMetrics()
        self._running = False
        self._phase = StressTestPhase.WARMUP
        self._start_time: Optional[float] = None
        self._lock = asyncio.Lock()

        self._order_latency = LatencyTracker()
        self._market_latency = LatencyTracker()
        self._position_latency = LatencyTracker()
        self._risk_latency = LatencyTracker()

        self.broker_manager: Optional[BrokerManager] = None
        self.execution_engine: Optional[LiveOrderExecutionEngine] = None
        self.risk_manager: Optional[LiveRiskManager] = None

        self._active_orders: Dict[str, ParentOrder] = {}
        self._market_subs: Set[str] = set()
        self._running_tasks: Set[asyncio.Task] = set()

        self._order_generator = OrderGenerator(config)

        self.on_phase_change: Optional[Callable[[StressTestPhase], None]] = None
        self.on_metrics_update: Optional[Callable[[StressTestMetrics], None]] = None
        self.on_complete: Optional[Callable[[StressTestMetrics], None]] = None

    async def setup(self):
        logger.info("Setting up stress test environment...")

        self.broker_manager = BrokerManager()

        for i in range(3):
            config = BrokerConfig(
                broker_type=BrokerType.SIMULATOR,
                name=f"mock_broker_{i}",
                account_id=f"STRESS_ACC_{i}",
                max_orders_per_second=5000,
            )
            broker = MockBroker(
                config,
                base_latency_ms=3.0,
                latency_jitter_ms=1.5,
                failure_rate=0.001,
                reject_rate=0.005,
            )
            self.broker_manager.register_broker(f"mock_broker_{i}", broker, default=(i==0))

        await self.broker_manager.connect_all()

        self.execution_engine = LiveOrderExecutionEngine(
            broker_manager=self.broker_manager,
            cost_model=CostModel.from_config(),
            execution_model=LiveExecutionModel(),
        )

        self.execution_engine.on_order_update = self._on_order_update
        self.execution_engine.on_slice_fill = self._on_slice_fill
        self.execution_engine.on_order_complete = self._on_order_complete

        self.risk_manager = LiveRiskManager(
            execution_engine=self.execution_engine,
            broker_manager=self.broker_manager,
        )

        await self.risk_manager.start()

        logger.info("Stress test environment ready")

    async def teardown(self):
        logger.info("Tearing down stress test environment...")

        for task in self._running_tasks:
            task.cancel()
        if self._running_tasks:
            await asyncio.gather(*self._running_tasks, return_exceptions=True)

        if self.risk_manager:
            await self.risk_manager.stop()

        if self.broker_manager:
            await self.broker_manager.disconnect_all()

        logger.info("Stress test environment cleaned up")

    async def run(self) -> StressTestMetrics:
        self._running = True
        self._start_time = time.time()

        await self._run_phase(StressTestPhase.WARMUP, self.config.ramp_up_seconds // 5)
        await self._run_phase(StressTestPhase.RAMP_UP, self.config.ramp_up_seconds)
        await self._run_phase(StressTestPhase.STEADY, self.config.steady_seconds)
        await self._run_phase(StressTestPhase.SPIKE, self.config.spike_duration)
        await self._run_phase(StressTestPhase.COOLDOWN, self.config.cooldown_seconds)

        self._running = False
        self._compute_final_metrics()

        if self.on_complete:
            self.on_complete(self.metrics)

        return self.metrics

    async def _run_phase(self, phase: StressTestPhase, duration: int):
        self._phase = phase
        logger.info(f"Starting phase: {phase.value} for {duration}s")

        if self.on_phase_change:
            self.on_phase_change(phase)

        phase_start = time.time()
        end_time = phase_start + duration

        if phase == StressTestPhase.WARMUP:
            target_qps = self.config.target_qps // 10
        elif phase == StressTestPhase.RAMP_UP:
            target_qps = self.config.target_qps
        elif phase == StressTestPhase.STEADY:
            target_qps = self.config.target_qps
        elif phase == StressTestPhase.SPIKE:
            target_qps = self.config.spike_qps
        else:
            target_qps = self.config.target_qps // 10

        load_task = asyncio.create_task(self._generate_load(target_qps, end_time))
        monitor_task = asyncio.create_task(self._monitor_metrics(end_time))

        self._running_tasks.add(load_task)
        self._running_tasks.add(monitor_task)

        try:
            await asyncio.gather(load_task, monitor_task, return_exceptions=True)
        finally:
            self._running_tasks.discard(load_task)
            self._running_tasks.discard(monitor_task)

    async def _generate_load(self, target_qps: int, end_time: float):
        interval = 1.0 / target_qps if target_qps > 0 else 1.0
        next_send = time.time()

        while time.time() < end_time and self._running:
            now = time.time()
            if now >= next_send:
                await self._send_random_order()
                next_send = now + interval
            else:
                await asyncio.sleep(min(0.001, next_send - now))

    async def _send_random_order(self):
        order = self._order_generator.generate()

        start = time.perf_counter()
        try:
            order_id = await self.execution_engine.submit_order(order)
            latency = (time.perf_counter() - start) * 1000

            async with self._lock:
                self.metrics.orders_sent += 1
                await self._order_latency.record(latency)

                self._active_orders[order.order_id] = order
        except Exception as e:
            async with self._lock:
                self.metrics.orders_failed += 1
                self.metrics.errors_by_type[type(e).__name__] += 1
            logger.debug(f"Order send failed: {e}")

    async def _monitor_metrics(self, end_time: float):
        while time.time() < end_time and self._running:
            await asyncio.sleep(1.0)
            await self._collect_metrics()

            if self.on_metrics_update:
                self.on_metrics_update(self.metrics)

    async def _collect_metrics(self):
        order_lat = self._order_latency.get_percentiles()
        self.metrics.order_latency_p50 = order_lat["p50"]
        self.metrics.order_latency_p95 = order_lat["p95"]
        self.metrics.order_latency_p99 = order_lat["p99"]
        self.metrics.order_latency_max = order_lat["max"]

        market_lat = self._market_latency.get_percentiles()
        self.metrics.market_latency_p50 = market_lat["p50"]
        self.metrics.market_latency_p99 = market_lat["p99"]

        position_lat = self._position_latency.get_percentiles()
        self.metrics.position_sync_latency_p99 = position_lat["p99"]

        risk_lat = self._risk_latency.get_percentiles()
        self.metrics.risk_calc_latency_p99 = risk_lat["p99"]

        import psutil
        process = psutil.Process()
        self.metrics.cpu_usage_pct = process.cpu_percent()
        self.metrics.memory_usage_mb = process.memory_info().rss / 1024 / 1024

    def _compute_final_metrics(self):
        self._collect_metrics()

    def _on_order_update(self, order: ParentOrder):
        pass

    def _on_slice_fill(self, parent: ParentOrder, slice_: OrderSlice, trade: Trade):
        async with self._lock:
            self.metrics.orders_filled += 1

    def _on_order_complete(self, order: ParentOrder):
        async with self._lock:
            if order.state == OrderState.FILLED:
                self.metrics.orders_acked += 1
            elif order.state in (OrderState.REJECTED, OrderState.CANCELLED):
                self.metrics.orders_rejected += 1
            self._active_orders.pop(order.order_id, None)


class OrderGenerator:
    def __init__(self, config: StressTestConfig):
        self.config = config
        self._strategies = [f"STRESS_STRAT_{i}" for i in range(config.strategy_count)]
        self._accounts = [f"STRESS_ACC_{i}" for i in range(config.account_count)]

    def generate(self) -> ParentOrder:
        symbol = random.choice(self.config.symbols)
        side = OrderSide.BUY if random.random() < 0.5 else OrderSide.SELL
        order_type = OrderType.LIMIT if random.random() < 0.7 else OrderType.MARKET
        price = random.uniform(*self.config.price_range)
        quantity = random.randint(*self.config.quantity_range)

        lot_size = 100
        quantity = (quantity // lot_size) * lot_size
        if quantity < 100:
            quantity = 100

        if order_type == OrderType.LIMIT:
            if side == OrderSide.BUY:
                price = round(price * random.uniform(0.995, 1.0), 2)
            else:
                price = round(price * random.uniform(1.0, 1.005), 2)

        return ParentOrder(
            order_id=f"STRESS_{uuid.uuid4().hex[:12]}",
            client_order_id=f"STRESS_{uuid.uuid4().hex[:12]}",
            symbol=symbol,
            side=side,
            total_shares=quantity,
            target_price=price,
            order_type=order_type,
            slice_config=SliceConfig(
                algorithm=SliceAlgorithm.TWAP,
                total_shares=quantity,
                max_slice_size=min(1000, quantity),
                time_horizon_seconds=300,
            ),
            strategy_id=random.choice(self._strategies),
            account_id=random.choice(self._accounts),
        )


class StressTestReport:
    @staticmethod
    def generate(metrics: StressTestMetrics, config: StressTestConfig) -> str:
        m = metrics.to_dict()

        report = f"""
========================================
        全链路实盘压测报告
========================================

【测试配置】
- 模式: {config.mode.value}
- 目标 QPS: {config.target_qps}
- 突刺 QPS: {config.spike_qps}
- 总时长: {config.duration_seconds}s
- 爬坡: {config.ramp_up_seconds}s
- 稳态: {config.steady_seconds}s
- 突刺: {config.spike_duration}s
- 并发策略: {config.strategy_count}
- 并发账户: {config.account_count}
- 测试品种: {len(config.symbols)} 只

【下单性能】
- 发送订单: {metrics['orders']['sent']}
- 收到确认: {metrics['orders']['acked']}
- 完全成交: {metrics['orders']['filled']}
- 被拒单: {metrics['orders']['rejected']}
- 发送失败: {metrics['orders']['failed']}
- 成功率: {metrics['orders']['success_rate']:.2%}

【延迟指标 (ms)】
- P50: {metrics['latency_ms']['p50']:.2f}
- P95: {metrics['latency_ms']['p95']:.2f}
- P99: {metrics['latency_ms']['p99']:.2f}
- Max: {metrics['latency_ms']['max']:.2f}

【行情数据】
- 推送 tick: {metrics['market_data']['ticks_received']}
- 延迟 P50: {metrics['market_data']['latency_p50']:.2f}ms
- 延迟 P99: {metrics['market_data']['latency_p99']:.2f}ms

【持仓同步】
- 同步次数: {metrics['position_sync']['count']}
- 延迟 P99: {metrics['position_sync']['latency_p99']:.2f}ms
- 差异数: {metrics['position_sync']['deltas']}

【风控计算】
- 计算次数: {metrics['risk_control']['calculations']}
- 延迟 P99: {metrics['risk_control']['latency_p99']:.2f}ms
- 拦截次数: {metrics['risk_control']['blocks']}

【系统资源】
- CPU: {metrics['system']['cpu_pct']:.1f}%
- 内存: {metrics['system']['memory_mb']:.1f} MB
- 网络: {metrics['system']['network_mbps']:.1f} Mbps

【错误统计】
- 连接错误: {metrics['errors']['connection']}
- 超时错误: {metrics['errors']['timeout']}
- 分类: {metrics['errors']['by_type']}

========================================
"""
        return report

    @staticmethod
    def save_json(metrics: StressTestMetrics, config: StressTestConfig, filepath: str):
        import json
        data = {
            "config": {
                "mode": config.mode.value,
                "target_qps": config.target_qps,
                "duration_seconds": config.duration_seconds,
            },
            "metrics": metrics.to_dict(),
            "timestamp": datetime.utcnow().isoformat(),
        }
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)


_stress_runner: Optional[StressTestRunner] = None


async def run_stress_test(config: StressTestConfig = None) -> StressTestMetrics:
    global _stress_runner
    if config is None:
        config = StressTestConfig()

    _stress_runner = StressTestRunner(config)
    await _stress_runner.setup()
    try:
        return await _stress_runner.run()
    finally:
        await _stress_runner.teardown()


def get_stress_runner() -> Optional[StressTestRunner]:
    return _stress_runner


import psutil
import json
from collections import defaultdict
from typing import Tuple
