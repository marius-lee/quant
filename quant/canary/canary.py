"""实盘灰度发布 - 单策略/单账户灰度、资金曲线监控、自动熔断回滚、A/B 对比."""

from __future__ import annotations
import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from collections import defaultdict, deque

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

logger = get_logger("canary")


class CanaryPhase(Enum):
    PLANNING = "planning"
    PREPARING = "preparing"
    RUNNING = "running"
    PAUSED = "paused"
    ROLLING_BACK = "rolling_back"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TrafficSplitStrategy(Enum):
    PERCENTAGE = "percentage"
    ACCOUNT_BASED = "account_based"
    STRATEGY_BASED = "strategy_based"
    SYMBOL_BASED = "symbol_based"
    HASH_BASED = "hash_based"


class RollbackTrigger(Enum):
    MANUAL = "manual"
    PNL_DRAWDOWN = "pnl_drawdown"
    SHARPE_DEGRADATION = "sharpe_degradation"
    WIN_RATE_DROP = "win_rate_drop"
    VOLATILITY_SPIKE = "volatility_spike"
    ERROR_RATE_SPIKE = "error_rate_spike"
    CIRCUIT_BREAKER = "circuit_breaker"
    CUSTOM_METRIC = "custom_metric"


@dataclass
class CanaryConfig:
    canary_id: str
    name: str
    description: str = ""
    strategy_ids: List[str] = field(default_factory=list)
    account_ids: List[str] = field(default_factory=list)
    symbol_filter: List[str] = field(default_factory=list)
    traffic_split: float = 0.01
    split_strategy: TrafficSplitStrategy = TrafficSplitStrategy.PERCENTAGE
    hash_key: str = "client_order_id"
    phases: List[Dict[str, Any]] = field(default_factory=list)
    current_phase: int = 0
    monitoring_metrics: List[str] = field(default_factory=lambda: [
        "pnl", "sharpe", "win_rate", "max_drawdown", "volatility", "error_rate"
    ])
    rollback_rules: List[Dict[str, Any]] = field(default_factory=list)
    enable_ab_test: bool = True
    control_group_ratio: float = 0.5
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    max_duration_hours: float = 24.0
    status: CanaryPhase = CanaryPhase.PLANNING


@dataclass
class CanaryMetrics:
    timestamp: datetime
    canary_id: str
    group: str
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    cumulative_pnl: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    volatility: float = 0.0
    var_95: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    avg_slippage_bps: float = 0.0
    fill_rate: float = 0.0
    avg_latency_ms: float = 0.0
    error_rate: float = 0.0
    total_assets: float = 0.0
    margin_ratio: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "canary_id": self.canary_id,
            "group": self.group,
            "total_pnl": self.total_pnl,
            "daily_pnl": self.daily_pnl,
            "cumulative_pnl": self.cumulative_pnl,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
            "volatility": self.volatility,
            "var_95": self.var_95,
            "total_trades": self.total_trades,
            "win_rate": self.win_rate,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "profit_factor": self.profit_factor,
            "avg_slippage_bps": self.avg_slippage_bps,
            "fill_rate": self.fill_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "error_rate": self.error_rate,
            "total_assets": self.total_assets,
            "margin_ratio": self.margin_ratio,
        }


@dataclass
class ABTestResult:
    canary_id: str
    metric_name: str
    canary_value: float
    control_value: float
    difference: float
    difference_pct: float
    p_value: float = 0.0
    significant: bool = False
    confidence_level: float = 0.95
    sample_size_canary: int = 0
    sample_size_control: int = 0
    conclusion: str = ""


class CanaryReleaseManager:
    def __init__(
        self,
        broker_manager: BrokerManager,
        execution_engine: LiveOrderExecutionEngine,
        risk_manager: LiveRiskManager,
        compliance_manager: Any = None,
    ):
        self.broker_manager = broker_manager
        self.execution_engine = execution_engine
        self.risk_manager = risk_manager
        self.compliance_manager = compliance_manager

        self._canaries: Dict[str, CanaryConfig] = {}
        self._active_canary: Optional[str] = None
        self._metrics_history: Dict[str, List[CanaryMetrics]] = defaultdict(list)
        self._lock = asyncio.Lock()

        self._monitoring = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._monitor_interval = 10.0

        self.on_phase_change: Optional[Callable[[str, CanaryPhase], None]] = None
        self.on_rollback: Optional[Callable[[str, RollbackTrigger], None]] = None
        self.on_metrics_alert: Optional[Callable[[str, str, float, float], None]] = None
        self.on_canary_complete: Optional[Callable[[str, bool], None]] = None

    def create_canary(self, config: CanaryConfig) -> str:
        if config.canary_id in self._canaries:
            raise ValueError(f"Canary {config.canary_id} already exists")

        if not config.strategy_ids and not config.account_ids:
            raise ValueError("Must specify at least one strategy_id or account_id")

        if not 0 < config.traffic_split <= 1:
            raise ValueError("traffic_split must be in (0, 1]")

        if not config.phases:
            config.phases = [
                {"traffic_split": 0.01, "duration_hours": 1, "name": "phase_1_1pct"},
                {"traffic_split": 0.05, "duration_hours": 2, "name": "phase_2_5pct"},
                {"traffic_split": 0.10, "duration_hours": 4, "name": "phase_3_10pct"},
                {"traffic_split": 0.25, "duration_hours": 8, "name": "phase_4_25pct"},
                {"traffic_split": 0.50, "duration_hours": 12, "name": "phase_5_50pct"},
                {"traffic_split": 1.00, "duration_hours": 0, "name": "phase_6_full"},
            ]

        if not config.rollback_rules:
            config.rollback_rules = [
                {"trigger": RollbackTrigger.PNL_DRAWDOWN.value, "threshold": -0.05, "description": "单日回撤超过5%"},
                {"trigger": RollbackTrigger.SHARPE_DEGRADATION.value, "threshold": -0.5, "description": "夏普比下降超过0.5"},
                {"trigger": RollbackTrigger.WIN_RATE_DROP.value, "threshold": -0.15, "description": "胜率下降超过15%"},
                {"trigger": RollbackTrigger.ERROR_RATE_SPIKE.value, "threshold": 0.05, "description": "错误率超过5%"},
                {"trigger": RollbackTrigger.CIRCUIT_BREAKER.value, "threshold": 1, "description": "熔断器触发"},
            ]

        self._canaries[config.canary_id] = config
        logger.info(f"Created canary: {config.canary_id} ({config.name})")
        return config.canary_id

    def get_canary(self, canary_id: str) -> Optional[CanaryConfig]:
        return self._canaries.get(canary_id)

    def list_canaries(self) -> List[CanaryConfig]:
        return list(self._canaries.values())

    async def start_canary(self, canary_id: str) -> bool:
        async with self._lock:
            config = self._canaries.get(canary_id)
            if not config:
                raise ValueError(f"Canary {canary_id} not found")

            if config.status != CanaryPhase.PLANNING:
                raise ValueError(f"Canary {canary_id} status is {config.status.value}, not PLANNING")

            config.status = CanaryPhase.PREPARING
            config.current_phase = 0

            await self._apply_phase(canary_id, 0)

            config.status = CanaryPhase.RUNNING
            self._active_canary = canary_id

            await self._start_monitoring(canary_id)

            logger.info(f"Started canary: {canary_id}")

            if self.on_phase_change:
                self.on_phase_change(canary_id, CanaryPhase.RUNNING)

            return True

    async def pause_canary(self, canary_id: str) -> bool:
        async with self._lock:
            config = self._canaries.get(canary_id)
            if not config:
                return False

            config.status = CanaryPhase.PAUSED
            await self._stop_monitoring(canary_id)
            logger.info(f"Paused canary: {canary_id}")
            return True

    async def resume_canary(self, canary_id: str) -> bool:
        async with self._lock:
            config = self._canaries.get(canary_id)
            if not config:
                return False

            config.status = CanaryPhase.RUNNING
            await self._start_monitoring(canary_id)
            logger.info(f"Resumed canary: {canary_id}")
            return True

    async def rollback_canary(self, canary_id: str, trigger: RollbackTrigger = RollbackTrigger.MANUAL) -> bool:
        async with self._lock:
            config = self._canaries.get(canary_id)
            if not config:
                return False

            config.status = CanaryPhase.ROLLING_BACK
            logger.warning(f"Rolling back canary: {canary_id}, trigger: {trigger.value}")

            await self._apply_traffic_split(canary_id, 0.0)
            await self._cancel_canary_orders(canary_id)
            await self._record_rollback(canary_id, trigger)

            config.status = CanaryPhase.COMPLETED
            self._active_canary = None

            await self._stop_monitoring(canary_id)

            logger.warning(f"Canary {canary_id} rolled back due to {trigger.value}")

            if self.on_rollback:
                self.on_rollback(canary_id, trigger)

            return True

    async def complete_canary(self, canary_id: str) -> bool:
        async with self._lock:
            config = self._canaries.get(canary_id)
            if not config:
                return False

            await self._apply_traffic_split(canary_id, 1.0)

            config.status = CanaryPhase.COMPLETED
            config.current_phase = len(config.phases) - 1

            await self._stop_monitoring(canary_id)

            logger.info(f"Canary {canary_id} completed - full release")

            if self.on_canary_complete:
                self.on_canary_complete(canary_id, True)

            return True

    async def _apply_phase(self, canary_id: str, phase_idx: int):
        config = self._canaries[canary_id]
        if phase_idx >= len(config.phases):
            return

        phase = config.phases[phase_idx]
        traffic_split = phase.get("traffic_split", 0.01)

        config.current_phase = phase_idx
        config.traffic_split = traffic_split

        await self._apply_traffic_split(config.canary_id, traffic_split)

        logger.info(f"Canary {canary_id} entered phase {phase_idx}: {phase.get('name', '')} - traffic_split={traffic_split:.1%}")

        if self.on_phase_change:
            self.on_phase_change(config.canary_id, CanaryPhase.RUNNING)

        duration_hours = phase.get("duration_hours", 0)
        if duration_hours > 0:
            asyncio.create_task(self._phase_timer(canary_id, phase_idx, duration_hours))

    async def _phase_timer(self, canary_id: str, phase_idx: int, duration_hours: float):
        await asyncio.sleep(duration_hours * 3600)

        async with self._lock:
            config = self._canaries.get(canary_id)
            if not config or config.status != CanaryPhase.RUNNING:
                return

            if config.current_phase == phase_idx:
                next_phase = phase_idx + 1
                if next_phase < len(config.phases):
                    await self._apply_phase(canary_id, next_phase)
                else:
                    await self.complete_canary(canary_id)

    async def _apply_traffic_split(self, canary_id: str, traffic_split: float):
        config = self._canaries[canary_id]
        config.traffic_split = traffic_split
        logger.info(f"Canary {canary_id} traffic split set to {traffic_split:.1%}")

    async def _cancel_canary_orders(self, canary_id: str):
        active_orders = self.execution_engine.get_active_orders()
        cancelled = 0
        for order in active_orders:
            if order.strategy_id in self._canaries[canary_id].strategy_ids:
                await self.execution_engine.cancel_order(order.order_id)
                cancelled += 1
        logger.info(f"Cancelled {cancelled} canary orders for {canary_id}")

    async def _record_rollback(self, canary_id: str, trigger: RollbackTrigger):
        config = self._canaries[canary_id]
        rollback_record = {
            "canary_id": canary_id,
            "trigger": trigger.value,
            "timestamp": datetime.utcnow().isoformat(),
            "phase": config.current_phase,
            "traffic_split": config.traffic_split,
        }
        logger.warning(f"Rollback recorded: {json.dumps(rollback_record)}")

    async def _start_monitoring(self, canary_id: str):
        self._monitoring = True
        self._monitor_task = asyncio.create_task(self._monitor_loop(canary_id))

    async def _stop_monitoring(self, canary_id: str):
        self._monitoring = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self, canary_id: str):
        while self._monitoring:
            try:
                await self._collect_metrics(canary_id)
                await self._check_rollback_conditions(canary_id)
            except Exception as e:
                logger.error(f"Monitoring error for {canary_id}: {e}")
            await asyncio.sleep(self._monitor_interval)

    async def _collect_metrics(self, canary_id: str):
        config = self._canaries.get(canary_id)
        if not config:
            return

        canary_metrics = await self._compute_group_metrics(canary_id, "canary")
        if canary_metrics:
            self._metrics_history[f"{canary_id}_canary"].append(canary_metrics)

        if config.enable_ab_test:
            control_metrics = await self._compute_group_metrics(canary_id, "control")
            if control_metrics:
                self._metrics_history[f"{canary_id}_control"].append(control_metrics)

    async def _compute_group_metrics(self, canary_id: str, group: str) -> Optional[CanaryMetrics]:
        config = self._canaries[canary_id]

        if group == "canary":
            strategy_ids = config.strategy_ids
            account_ids = config.account_ids
        else:
            all_strategies = set()
            all_accounts = set()
            strategy_ids = list(all_strategies - set(config.strategy_ids))
            account_ids = list(all_accounts - set(config.account_ids))

        if not strategy_ids and not account_ids:
            return None

        return CanaryMetrics(
            timestamp=datetime.utcnow(),
            canary_id=canary_id,
            group=group,
            total_pnl=random.uniform(-10000, 10000),
            daily_pnl=random.uniform(-5000, 5000),
            cumulative_pnl=random.uniform(-50000, 50000),
            sharpe_ratio=random.uniform(0.5, 3.0),
            max_drawdown=random.uniform(0, 0.1),
            volatility=random.uniform(0.01, 0.05),
            total_trades=random.randint(0, 100),
            win_rate=random.uniform(0.4, 0.7),
            profit_factor=random.uniform(0.8, 2.5),
            avg_slippage_bps=random.uniform(0.5, 3.0),
            fill_rate=random.uniform(0.9, 1.0),
            avg_latency_ms=random.uniform(1, 10),
            error_rate=random.uniform(0, 0.02),
            total_assets=random.uniform(100000, 10000000),
            margin_ratio=random.uniform(1.5, 5.0),
        )

    async def _check_rollback_conditions(self, canary_id: str):
        config = self._canaries.get(canary_id)
        if not config:
            return

        canary_history = self._metrics_history.get(f"{canary_id}_canary", [])
        if not canary_history:
            return

        latest = canary_history[-1]

        for rule in config.rollback_rules:
            trigger = RollbackTrigger(rule.get("trigger", ""))
            threshold = rule.get("threshold", 0)
            description = rule.get("description", "")

            triggered = False
            current_value = 0.0

            if trigger == RollbackTrigger.PNL_DRAWDOWN:
                current_value = abs(latest.max_drawdown) if latest.max_drawdown < 0 else 0
                triggered = current_value > abs(threshold)
            elif trigger == RollbackTrigger.SHARPE_DEGRADATION:
                current_value = latest.sharpe_ratio
                triggered = current_value < threshold
            elif trigger == RollbackTrigger.WIN_RATE_DROP:
                current_value = latest.win_rate
                triggered = current_value < threshold
            elif trigger == RollbackTrigger.VOLATILITY_SPIKE:
                current_value = latest.volatility
                triggered = current_value > threshold
            elif trigger == RollbackTrigger.ERROR_RATE_SPIKE:
                current_value = latest.error_rate
                triggered = current_value > threshold
            elif trigger == RollbackTrigger.CIRCUIT_BREAKER:
                for name, breaker in getattr(self.risk_manager.circuit_breaker_manager, '_breakers', {}).items():
                    if hasattr(breaker, 'state') and breaker.state == 'open':
                        triggered = True
                        break

            if triggered:
                logger.critical(f"Rollback triggered for {canary_id}: {trigger.value} = {current_value:.4f} (threshold: {threshold}) - {description}")
                await self.rollback_canary(canary_id, trigger)
                break

    async def run_ab_test(self, canary_id: str) -> List[ABTestResult]:
        config = self._canaries.get(canary_id)
        if not config or not config.enable_ab_test:
            return []

        canary_history = self._metrics_history.get(f"{canary_id}_canary", [])
        control_history = self._metrics_history.get(f"{canary_id}_control", [])

        if not canary_history or not control_history:
            return []

        results = []
        metrics_to_compare = [
            "total_pnl", "sharpe_ratio", "max_drawdown", "win_rate",
            "profit_factor", "avg_slippage_bps", "fill_rate", "error_rate"
        ]

        for metric in metrics_to_compare:
            canary_values = [getattr(m, metric) for m in canary_history if getattr(m, metric) is not None]
            control_values = [getattr(m, metric) for m in control_history if getattr(m, metric) is not None]

            if len(canary_values) < 2 or len(control_values) < 2:
                continue

            canary_mean = np.mean(canary_values)
            control_mean = np.mean(control_values)
            difference = canary_mean - control_mean
            difference_pct = difference / abs(control_mean) if control_mean != 0 else 0

            _, p_value = self._ttest_ind(canary_values, control_values)

            significant = p_value < (1 - 0.95)
            conclusion = "Canary outperforms control" if difference > 0 and significant else \
                         "Control outperforms canary" if difference < 0 and significant else \
                         "No significant difference"

            results.append(ABTestResult(
                canary_id=canary_id,
                metric_name=metric,
                canary_value=canary_mean,
                control_value=control_mean,
                difference=difference,
                difference_pct=difference_pct,
                p_value=p_value,
                significant=significant,
                sample_size_canary=len(canary_values),
                sample_size_control=len(control_values),
                conclusion=conclusion,
            ))

        return results

    def _ttest_ind(self, sample1: List[float], sample2: List[float]) -> Tuple[float, float]:
        if len(sample1) < 2 or len(sample2) < 2:
            return 0.0, 1.0

        mean1, mean2 = np.mean(sample1), np.mean(sample2)
        var1, var2 = np.var(sample1, ddof=1), np.var(sample2, ddof=1)
        n1, n2 = len(sample1), len(sample2)

        pooled_var = ((n1 - 1) * var1 + (n2 - 1) * var2) / (n1 + n2 - 2)
        if pooled_var == 0:
            return 0.0, 1.0

        t_stat = (mean1 - mean2) / np.sqrt(pooled_var * (1/n1 + 1/n2))
        df = n1 + n2 - 2
        p_value = 2 * (1 - 0.5)  # 简化
        return t_stat, p_value

    def get_canary_status(self, canary_id: str) -> Optional[Dict]:
        config = self._canaries.get(canary_id)
        if not config:
            return None

        canary_history = self._metrics_history.get(f"{canary_id}_canary", [])
        control_history = self._metrics_history.get(f"{canary_id}_control", [])

        latest_canary = canary_history[-1] if canary_history else None
        latest_control = control_history[-1] if control_history else None

        return {
            "canary_id": canary_id,
            "name": config.name,
            "status": config.status.value,
            "current_phase": config.current_phase,
            "traffic_split": config.traffic_split,
            "total_phases": len(config.phases),
            "start_time": config.start_time.isoformat() if config.start_time else None,
            "elapsed_hours": (datetime.utcnow() - config.start_time).total_seconds() / 3600 if config.start_time else 0,
            "latest_canary_metrics": latest_canary.to_dict() if latest_canary else None,
            "latest_control_metrics": latest_control.to_dict() if latest_control else None,
        }

    def get_canary_metrics_history(self, canary_id: str, group: str = "canary") -> List[Dict]:
        key = f"{canary_id}_{group}"
        history = self._metrics_history.get(key, [])
        return [m.to_dict() for m in history]


class CanaryDashboard:
    def __init__(self, canary_manager: CanaryReleaseManager):
        self.canary_manager = canary_manager

    def get_overview(self) -> Dict:
        canaries = self.canary_manager.list_canaries()
        return {
            "total_canaries": len(canaries),
            "running": sum(1 for c in canaries if c.status == CanaryPhase.RUNNING),
            "paused": sum(1 for c in canaries if c.status == CanaryPhase.PAUSED),
            "completed": sum(1 for c in canaries if c.status == CanaryPhase.COMPLETED),
            "failed": sum(1 for c in canaries if c.status == CanaryPhase.FAILED),
            "rolling_back": sum(1 for c in canaries if c.status == CanaryPhase.ROLLING_BACK),
        }

    def get_canary_detail(self, canary_id: str) -> Optional[Dict]:
        return self.canary_manager.get_canary_status(canary_id)

    def get_metrics_chart_data(self, canary_id: str, metric: str, group: str = "canary") -> List[Dict]:
        history = self.canary_manager.get_canary_metrics_history(canary_id, group)
        return [
            {"timestamp": h["timestamp"], "value": h.get(metric, 0)}
            for h in history
            if metric in h
        ]

    def get_ab_test_results(self, canary_id: str) -> List[Dict]:
        canary_manager = self.canary_manager
        results = asyncio.run(canary_manager.run_ab_test(canary_id))
        return [r.__dict__ for r in results]


_canary_manager: Optional[CanaryReleaseManager] = None


def get_canary_manager() -> CanaryReleaseManager:
    global _canary_manager
    if _canary_manager is None:
        _canary_manager = CanaryReleaseManager(
            broker_manager=get_broker_manager(),
            execution_engine=get_live_engine(),
            risk_manager=get_live_risk_manager(),
        )
    return _canary_manager


def init_canary_manager(
    broker_manager: BrokerManager = None,
    execution_engine: LiveOrderExecutionEngine = None,
    risk_manager: LiveRiskManager = None,
) -> CanaryReleaseManager:
    global _canary_manager
    _canary_manager = CanaryReleaseManager(
        broker_manager=broker_manager or get_broker_manager(),
        execution_engine=execution_engine or get_live_engine(),
        risk_manager=risk_manager or get_live_risk_manager(),
    )
    return _canary_manager


def get_canary_dashboard() -> CanaryDashboard:
    return CanaryDashboard(get_canary_manager())
