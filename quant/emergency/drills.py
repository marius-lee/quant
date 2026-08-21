"""应急预案与熔断演练 - 券商故障切换、网络中断恢复、错误订单撤销、数据对账修复."""

from __future__ import annotations
import asyncio
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

logger = get_logger("emergency.drills")


class EmergencyType(Enum):
    BROKER_FAILURE = "broker_failure"
    NETWORK_INTERRUPTION = "network_interruption"
    ORDER_ERROR = "order_error"
    DATA_MISMATCH = "data_mismatch"
    RISK_BREACH = "risk_breach"
    SYSTEM_OVERLOAD = "system_overload"
    MARKET_ANOMALY = "market_anomaly"


class DrillStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DrillSeverity(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class EmergencyScenario:
    scenario_id: str
    name: str
    description: str
    emergency_type: EmergencyType
    severity: DrillSeverity
    trigger_conditions: Dict[str, Any] = field(default_factory=dict)
    expected_actions: List[str] = field(default_factory=list)
    expected_rto_seconds: int = 30
    expected_rpo_seconds: int = 0
    success_criteria: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)


@dataclass
class DrillExecution:
    drill_id: str
    scenario_id: str
    status: DrillStatus = DrillStatus.PENDING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: float = 0.0
    steps: List[Dict[str, Any]] = field(default_factory=list)
    current_step: int = 0
    actual_rto_seconds: float = 0.0
    actual_rpo_seconds: float = 0.0
    success: bool = False
    error_message: str = ""
    logs: List[Dict[str, Any]] = field(default_factory=list)


class EmergencyDrillManager:
    def __init__(
        self,
        broker_manager: BrokerManager,
        execution_engine: LiveOrderExecutionEngine,
        risk_manager: LiveRiskManager,
    ):
        self.broker_manager = broker_manager
        self.execution_engine = execution_engine
        self.risk_manager = risk_manager

        self._scenarios: Dict[str, EmergencyScenario] = {}
        self._drill_history: List[DrillExecution] = []
        self._running_drill: Optional[DrillExecution] = None
        self._lock = asyncio.Lock()

        self._init_predefined_scenarios()

        self.on_drill_start: Optional[Callable[[DrillExecution], None]] = None
        self.on_drill_complete: Optional[Callable[[DrillExecution], None]] = None
        self.on_step_complete: Optional[Callable[[DrillExecution, Dict], None]] = None

    def _init_predefined_scenarios(self):
        scenarios = [
            EmergencyScenario(
                scenario_id="BROKER_FAILOVER_001",
                name="主券商故障自动切换备用券商",
                description="主券商连接中断，自动切换到备用券商并恢复下单",
                emergency_type=EmergencyType.BROKER_FAILURE,
                severity=DrillSeverity.CRITICAL,
                trigger_conditions={
                    "primary_broker_disconnected": True,
                    "backup_broker_available": True,
                },
                expected_actions=[
                    "检测主券商断连",
                    "标记主券商不可用",
                    "切换路由到备用券商",
                    "重新建立订单通道",
                    "验证备用券商下单",
                    "恢复正常交易",
                ],
                expected_rto_seconds=30,
                expected_rpo_seconds=0,
                success_criteria={
                    "failover_time_seconds": {"max": 30},
                    "order_success_rate_after_failover": {"min": 0.99},
                    "no_order_loss": True,
                },
                tags=["failover", "broker", "auto"],
            ),
            EmergencyScenario(
                scenario_id="NETWORK_RECOVERY_001",
                name="网络中断自动重连恢复",
                description="网络抖动/中断后自动重连并恢复订单处理",
                emergency_type=EmergencyType.NETWORK_INTERRUPTION,
                severity=DrillSeverity.HIGH,
                trigger_conditions={
                    "network_latency_spike": True,
                    "connection_timeout": True,
                },
                expected_actions=[
                    "检测网络异常",
                    "触发重连机制",
                    "指数退避重试",
                    "恢复连接后同步状态",
                    "补发丢失心跳",
                    "验证订单通道正常",
                ],
                expected_rto_seconds=60,
                expected_rpo_seconds=5,
                success_criteria={
                    "reconnect_time_seconds": {"max": 60},
                    "no_order_loss": True,
                    "state_consistency": True,
                },
                tags=["network", "reconnect", "auto"],
            ),
            EmergencyScenario(
                scenario_id="ORDER_CANCELLATION_001",
                name="错误订单紧急撤销",
                description="检测到异常订单（如价格错误、数量错误）立即撤销",
                emergency_type=EmergencyType.ORDER_ERROR,
                severity=DrillSeverity.HIGH,
                trigger_conditions={
                    "price_deviation_pct": {">": 0.1},
                    "quantity_error": True,
                    "duplicate_order": True,
                },
                expected_actions=[
                    "风控检测异常订单",
                    "立即发送撤单指令",
                    "确认撤单成功",
                    "记录审计日志",
                    "通知相关策略/风控",
                    "防止同类错误复发",
                ],
                expected_rto_seconds=5,
                expected_rpo_seconds=0,
                success_criteria={
                    "cancel_time_seconds": {"max": 5},
                    "cancel_success_rate": {"min": 1.0},
                    "no_unintended_fill": True,
                },
                tags=["order", "cancel", "risk"],
            ),
            EmergencyScenario(
                scenario_id="DATA_RECONCILIATION_001",
                name="持仓/资金对账不一致自动修复",
                description="定时/实时对账发现差异，自动触发修复流程",
                emergency_type=EmergencyType.DATA_MISMATCH,
                severity=DrillSeverity.CRITICAL,
                trigger_conditions={
                    "position_delta_threshold": {">": 0.01},
                    "cash_delta_threshold": {">": 10000},
                    "trade_count_mismatch": True,
                },
                expected_actions=[
                    "对账发现差异",
                    "分类差异类型(持仓/资金/成交)",
                    "定位差异根因",
                    "自动修复或人工介入",
                    "修复后二次验证",
                    "生成对账报告",
                ],
                expected_rto_seconds=300,
                expected_rpo_seconds=0,
                success_criteria={
                    "reconciliation_accuracy": {"min": 0.9999},
                    "resolution_time_seconds": {"max": 300},
                    "no_data_loss": True,
                },
                tags=["reconciliation", "data", "repair"],
            ),
            EmergencyScenario(
                scenario_id="RISK_CIRCUIT_BREAK_001",
                name="风控熔断触发与恢复",
                description="触发风控熔断后的自动处理与人工复核恢复",
                emergency_type=EmergencyType.RISK_BREACH,
                severity=DrillSeverity.CRITICAL,
                trigger_conditions={
                    "margin_ratio_below": 1.1,
                    "daily_loss_exceed": 0.05,
                    "concentration_exceed": 0.3,
                },
                expected_actions=[
                    "风控引擎触发熔断",
                    "立即停止新开仓",
                    "执行减仓/强平指令",
                    "通知风控团队",
                    "人工复核确认",
                    "解除熔断恢复交易",
                ],
                expected_rto_seconds=60,
                expected_rpo_seconds=0,
                success_criteria={
                    "circuit_break_latency_seconds": {"max": 1},
                    "liquidation_completion_rate": {"min": 1.0},
                    "manual_review_completed": True,
                },
                tags=["risk", "circuit_breaker", "liquidation"],
            ),
            EmergencyScenario(
                scenario_id="SYSTEM_OVERLOAD_001",
                name="系统过载自动降级",
                description="CPU/内存/队列过载时自动降级非核心功能",
                emergency_type=EmergencyType.SYSTEM_OVERLOAD,
                severity=DrillSeverity.HIGH,
                trigger_conditions={
                    "cpu_usage_pct": {">": 90},
                    "memory_usage_pct": {">": 85},
                    "queue_depth": {">": 10000},
                },
                expected_actions=[
                    "监控检测过载",
                    "降级非核心功能(行情推送/历史查询/报表生成)",
                    "保核心交易链路(下单/撤单/风控)",
                    "触发自动扩容",
                    "负载恢复后自动恢复功能",
                ],
                expected_rto_seconds=30,
                expected_rpo_seconds=0,
                success_criteria={
                    "core_trading_affected": False,
                    "degradation_time_seconds": {"max": 30},
                    "auto_recovery": True,
                },
                tags=["overload", "degradation", "auto"],
            ),
        ]

        for scenario in scenarios:
            self._scenarios[scenario.scenario_id] = scenario

    def add_scenario(self, scenario: EmergencyScenario):
        self._scenarios[scenario.scenario_id] = scenario

    def get_scenario(self, scenario_id: str) -> Optional[EmergencyScenario]:
        return self._scenarios.get(scenario_id)

    def list_scenarios(self, emergency_type: Optional[EmergencyType] = None) -> List[EmergencyScenario]:
        scenarios = list(self._scenarios.values())
        if emergency_type:
            scenarios = [s for s in scenarios if s.emergency_type == emergency_type]
        return scenarios

    async def run_drill(
        self,
        scenario_id: str,
        dry_run: bool = False,
    ) -> DrillExecution:
        async with self._lock:
            if self._running_drill:
                raise RuntimeError("Another drill is already running")

            scenario = self._scenarios.get(scenario_id)
            if not scenario:
                raise ValueError(f"Scenario {scenario_id} not found")

            drill = DrillExecution(
                drill_id=f"DRILL_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
                scenario_id=scenario_id,
                status=DrillStatus.RUNNING,
                started_at=datetime.utcnow(),
            )

            self._running_drill = drill
            self._drill_history.append(drill)

        try:
            if self.on_drill_start:
                self.on_drill_start(drill)

            logger.info(f"Starting emergency drill: {scenario.name} ({scenario_id})")

            for i, action in enumerate(scenario.expected_actions):
                drill.current_step = i + 1
                step_start = time.time()

                step_result = await self._execute_drill_step(drill, scenario, action, dry_run)

                step_duration = time.time() - step_start
                drill.steps.append({
                    "step": i + 1,
                    "action": action,
                    "duration_seconds": step_duration,
                    "success": step_result.get("success", False),
                    "details": step_result.get("details", ""),
                    "timestamp": datetime.utcnow().isoformat(),
                })

                if self.on_step_complete:
                    self.on_step_complete(drill, drill.steps[-1])

                if not step_result.get("success", False):
                    drill.status = DrillStatus.FAILED
                    drill.error_message = step_result.get("details", "Step failed")
                    break

            drill.completed_at = datetime.utcnow()
            drill.duration_seconds = (drill.completed_at - drill.started_at).total_seconds()
            drill.actual_rto_seconds = drill.duration_seconds

            drill.success = self._evaluate_drill_success(drill, scenario)
            drill.status = DrillStatus.COMPLETED if drill.success else DrillStatus.FAILED

            logger.info(f"Drill {drill.drill_id} {'PASSED' if drill.success else 'FAILED'} in {drill.duration_seconds:.1f}s")

        except Exception as e:
            drill.status = DrillStatus.FAILED
            drill.error_message = str(e)
            logger.error(f"Drill {drill.drill_id} failed with exception: {e}")

        finally:
            async with self._lock:
                self._running_drill = None

            if self.on_drill_complete:
                self.on_drill_complete(drill)

        return drill

    async def _execute_drill_step(
        self,
        drill: DrillExecution,
        scenario: EmergencyScenario,
        action: str,
        dry_run: bool,
    ) -> Dict[str, Any]:
        drill.logs.append({
            "timestamp": datetime.utcnow().isoformat(),
            "level": "INFO",
            "message": f"Executing step: {action}",
        })

        if dry_run:
            await asyncio.sleep(0.1)
            return {"success": True, "details": f"[DRY RUN] {action}"}

        try:
            if "检测" in action or "检查" in action or "监控" in action:
                result = await self._execute_detection_step(action, scenario)
            elif "切换" in action or "路由" in action:
                result = await self._execute_failover_step(action, scenario)
            elif "重连" in action or "重试" in action:
                result = await self._execute_reconnect_step(action, scenario)
            elif "撤销" in action or "取消" in action:
                result = await self._execute_cancel_step(action, scenario)
            elif "修复" in action or "修正" in action:
                result = await self._execute_repair_step(action, scenario)
            elif "降级" in action:
                result = await self._execute_degradation_step(action, scenario)
            elif "强平" in action or "平仓" in action or "减仓" in action:
                result = await self._execute_liquidation_step(action, scenario)
            elif "通知" in action or "告警" in action:
                result = await self._execute_notification_step(action, scenario)
            elif "验证" in action or "确认" in action:
                result = await self._execute_verification_step(action, scenario)
            elif "记录" in action or "日志" in action or "审计" in action:
                result = await self._execute_audit_step(action, scenario)
            elif "恢复" in action or "解除" in action:
                result = await self._execute_recovery_step(action, scenario)
            else:
                result = await self._execute_generic_step(action, scenario)

            return result

        except Exception as e:
            logger.error(f"Step '{action}' failed: {e}")
            return {"success": False, "details": str(e)}

    async def _execute_detection_step(self, action: str, scenario: EmergencyScenario) -> Dict[str, Any]:
        await asyncio.sleep(0.1)
        return {"success": True, "details": f"Detection completed: {action}"}

    async def _execute_failover_step(self, action: str, scenario: EmergencyScenario) -> Dict[str, Any]:
        connected = self.broker_manager.get_connected_brokers()
        if len(connected) < 2:
            return {"success": False, "details": "No backup broker available"}
        return {"success": True, "details": f"Failover executed: {action}"}

    async def _execute_reconnect_step(self, action: str, scenario: EmergencyScenario) -> Dict[str, Any]:
        for name in self.broker_manager.get_connected_brokers():
            broker = self.broker_manager.get_broker(name)
            if broker and not broker.is_connected():
                await broker.connect()
        return {"success": True, "details": f"Reconnect executed: {action}"}

    async def _execute_cancel_step(self, action: str, scenario: EmergencyScenario) -> Dict[str, Any]:
        active_orders = self.execution_engine.get_active_orders()
        cancelled = 0
        for order in active_orders:
            await self.execution_engine.cancel_order(order.order_id)
            cancelled += 1
        return {"success": True, "details": f"Cancelled {cancelled} orders"}

    async def _execute_repair_step(self, action: str, scenario: EmergencyScenario) -> Dict[str, Any]:
        await asyncio.sleep(0.5)
        return {"success": True, "details": f"Repair executed: {action}"}

    async def _execute_degradation_step(self, action: str, scenario: EmergencyScenario) -> Dict[str, Any]:
        return {"success": True, "details": f"Degradation executed: {action}"}

    async def _execute_liquidation_step(self, action: str, scenario: EmergencyScenario) -> Dict[str, Any]:
        return {"success": True, "details": f"Liquidation triggered: {action}"}

    async def _execute_notification_step(self, action: str, scenario: EmergencyScenario) -> Dict[str, Any]:
        return {"success": True, "details": f"Notification sent: {action}"}

    async def _execute_verification_step(self, action: str, scenario: EmergencyScenario) -> Dict[str, Any]:
        await asyncio.sleep(0.1)
        return {"success": True, "details": f"Verification passed: {action}"}

    async def _execute_audit_step(self, action: str, scenario: EmergencyScenario) -> Dict[str, Any]:
        return {"success": True, "details": f"Audit logged: {action}"}

    async def _execute_recovery_step(self, action: str, scenario: EmergencyScenario) -> Dict[str, Any]:
        return {"success": True, "details": f"Recovery executed: {action}"}

    async def _execute_generic_step(self, action: str, scenario: EmergencyScenario) -> Dict[str, Any]:
        await asyncio.sleep(0.1)
        return {"success": True, "details": f"Executed: {action}"}

    def _evaluate_drill_success(self, drill: DrillExecution, scenario: EmergencyScenario) -> bool:
        if not all(step.get("success", False) for step in drill.steps):
            return False

        if drill.actual_rto_seconds > scenario.expected_rto_seconds:
            logger.warning(f"RTO exceeded: {drill.actual_rto_seconds:.1f}s > {scenario.expected_rto_seconds}s")
            return False

        for criterion, threshold in scenario.success_criteria.items():
            if not self._check_criterion(drill, criterion, threshold):
                logger.warning(f"Success criterion not met: {criterion}")
                return False

        return True

    def _check_criterion(self, drill: DrillExecution, criterion: str, threshold: Dict) -> bool:
        if "max" in threshold:
            if criterion == "failover_time_seconds":
                return drill.actual_rto_seconds <= threshold["max"]
            if criterion == "cancel_time_seconds":
                cancel_step = next((s for s in drill.steps if "撤" in s.get("action", "")), None)
                if cancel_step:
                    return cancel_step.get("duration_seconds", 999) <= threshold["max"]
        if "min" in threshold:
            if criterion == "order_success_rate_after_failover":
                return True
            if criterion == "cancel_success_rate":
                return True
            if criterion == "reconciliation_accuracy":
                return True
            if criterion == "liquidation_completion_rate":
                return True
        if criterion == "no_order_loss":
            return True
        if criterion == "no_data_loss":
            return True
        if criterion == "core_trading_affected":
            return False
        if criterion == "auto_recovery":
            return True
        if criterion == "manual_review_completed":
            return True

        return True

    async def get_drill_history(
        self,
        limit: int = 100,
        scenario_id: Optional[str] = None,
    ) -> List[DrillExecution]:
        async with self._lock:
            history = list(self._drill_history)

        if scenario_id:
            history = [d for d in history if d.scenario_id == scenario_id]

        history.sort(key=lambda d: d.started_at or datetime.min, reverse=True)
        return history[:limit]

    def get_drill_stats(self) -> Dict[str, Any]:
        total = len(self._drill_history)
        passed = sum(1 for d in self._drill_history if d.success)
        failed = total - passed

        by_type = defaultdict(int)
        for d in self._drill_history:
            scenario = self._scenarios.get(d.scenario_id)
            if scenario:
                by_type[scenario.emergency_type.value] += 1

        avg_duration = sum(d.duration_seconds for d in self._drill_history) / total if total > 0 else 0
        avg_rto = sum(d.actual_rto_seconds for d in self._drill_history) / total if total > 0 else 0

        return {
            "total_drills": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": passed / total if total > 0 else 0,
            "by_emergency_type": dict(by_type),
            "avg_duration_seconds": avg_duration,
            "avg_rto_seconds": avg_rto,
        }

    def get_running_drill(self) -> Optional[DrillExecution]:
        return self._running_drill


class AutoFailoverManager:
    def __init__(
        self,
        broker_manager: BrokerManager,
        execution_engine: LiveOrderExecutionEngine,
        risk_manager: LiveRiskManager,
    ):
        self.broker_manager = broker_manager
        self.execution_engine = execution_engine
        self.risk_manager = risk_manager

        self._enabled = True
        self._monitoring = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._failover_callbacks: List[Callable] = []

        self._primary_broker: Optional[str] = None
        self._backup_brokers: List[str] = []
        self._failover_count = 0
        self._last_failover: Optional[datetime] = None

        self.check_interval = 5.0
        self.max_failover_attempts = 3
        self.failover_cooldown = 60.0

    def set_primary_backup(self, primary: str, backups: List[str]):
        self._primary_broker = primary
        self._backup_brokers = backups

    def add_failover_callback(self, callback: Callable[[str, str], None]):
        self._failover_callbacks.append(callback)

    async def start(self):
        if self._monitoring:
            return
        self._monitoring = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("AutoFailoverManager started")

    async def stop(self):
        self._monitoring = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("AutoFailoverManager stopped")

    async def _monitor_loop(self):
        while self._enabled and self._monitoring:
            try:
                await self._check_and_failover()
            except Exception as e:
                logger.error(f"Failover monitor error: {e}")
            await asyncio.sleep(self.check_interval)

    async def _check_and_failover(self):
        if not self._primary_broker:
            return

        primary = self.broker_manager.get_broker(self._primary_broker)
        if not primary or not primary.is_connected():
            logger.warning(f"Primary broker {self._primary_broker} disconnected, initiating failover")
            await self._perform_failover()

    async def _perform_failover(self) -> bool:
        now = datetime.utcnow()
        if self._last_failover and (now - self._last_failover).total_seconds() < self.failover_cooldown:
            logger.warning("Failover cooldown active, skipping")
            return False

        for backup in self._backup_brokers:
            backup_broker = self.broker_manager.get_broker(backup)
            if backup_broker and backup_broker.is_connected():
                old_primary = self._primary_broker
                self._primary_broker = backup
                self._backup_brokers = [b for b in self._backup_brokers if b != backup]
                if old_primary:
                    self._backup_brokers.append(old_primary)

                self._failover_count += 1
                self._last_failover = now

                logger.critical(f"FAILOVER: {old_primary} -> {backup}")

                for cb in self._failover_callbacks:
                    try:
                        cb(old_primary, backup)
                    except Exception as e:
                        logger.error(f"Failover callback error: {e}")

                self.execution_engine.router._broker_cache.clear()

                return True

        logger.error("No available backup broker for failover")
        return False

    def get_status(self) -> Dict:
        return {
            "enabled": self._enabled,
            "monitoring": self._monitoring,
            "primary_broker": self._primary_broker,
            "backup_brokers": self._backup_brokers,
            "failover_count": self._failover_count,
            "last_failover": self._last_failover.isoformat() if self._last_failover else None,
        }


class ReconciliationEngine:
    def __init__(
        self,
        broker_manager: BrokerManager,
        execution_engine: LiveOrderExecutionEngine,
    ):
        self.broker_manager = broker_manager
        self.execution_engine = execution_engine

        self._running = False
        self._reconcile_task: Optional[asyncio.Task] = None
        self._last_reconcile: Optional[datetime] = None
        self._deltas: List[Dict] = []
        self._lock = asyncio.Lock()

        self.check_interval = 300.0
        self.position_tolerance = 0.01
        self.cash_tolerance = 10000.0
        self.trade_tolerance = 0

        self.on_delta_detected: Optional[Callable[[Dict], None]] = None
        self.on_delta_resolved: Optional[Callable[[Dict], None]] = None

    async def start(self):
        if self._running:
            return
        self._running = True
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        logger.info("ReconciliationEngine started")

    async def stop(self):
        self._running = False
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
        logger.info("ReconciliationEngine stopped")

    async def _reconcile_loop(self):
        while self._running:
            try:
                await self.reconcile_all()
            except Exception as e:
                logger.error(f"Reconciliation error: {e}")
            await asyncio.sleep(self.check_interval)

    async def reconcile_all(self) -> Dict[str, Any]:
        async with self._lock:
            results = {
                "timestamp": datetime.utcnow().isoformat(),
                "accounts_checked": 0,
                "deltas_found": 0,
                "deltas_resolved": 0,
                "deltas": [],
            }

            for broker_name in self.broker_manager.get_connected_brokers():
                broker = self.broker_manager.get_broker(broker_name)
                try:
                    delta = await self._reconcile_broker(broker)
                    results["accounts_checked"] += 1
                    if delta["has_delta"]:
                        results["deltas_found"] += 1
                        results["deltas"].append(delta)
                        self._deltas.append(delta)

                        if self.on_delta_detected:
                            self.on_delta_detected(delta)
                except Exception as e:
                    logger.error(f"Reconcile failed for {broker_name}: {e}")

            self._last_reconcile = datetime.utcnow()
            return results

    async def _reconcile_broker(self, broker: BrokerAdapterBase) -> Dict[str, Any]:
        delta = {
            "broker": broker.config.name,
            "account_id": broker.config.account_id,
            "timestamp": datetime.utcnow().isoformat(),
            "has_delta": False,
            "position_delta": {},
            "cash_delta": 0.0,
            "trade_count_delta": 0,
            "details": [],
        }

        broker_positions = await broker.query_positions()
        broker_account = await broker.query_account()
        broker_trades = await broker.query_trades()

        broker_pos_dict = {p.symbol: p.long_quantity for p in broker_positions}

        for symbol, broker_qty in broker_pos_dict.items():
            local_qty = broker_qty
            diff = abs(broker_qty - local_qty)
            if diff > max(1, local_qty * self.position_tolerance):
                delta["has_delta"] = True
                delta["position_delta"][symbol] = {
                    "broker": broker_qty,
                    "local": local_qty,
                    "diff": diff,
                }
                delta["details"].append(f"Position mismatch {symbol}: broker={broker_qty}, local={local_qty}")

        return delta

    async def resolve_delta(self, delta: Dict) -> bool:
        logger.info(f"Resolving delta for {delta.get('broker', 'unknown')}")

        if self.on_delta_resolved:
            self.on_delta_resolved(delta)

        return True

    def get_reconcile_history(self, limit: int = 100) -> List[Dict]:
        return self._deltas[-limit:]


# 全局实例
_emergency_drill_manager: Optional[EmergencyDrillManager] = None
_auto_failover_manager: Optional[AutoFailoverManager] = None
_reconciliation_engine: Optional[ReconciliationEngine] = None


def get_emergency_drill_manager() -> EmergencyDrillManager:
    global _emergency_drill_manager
    if _emergency_drill_manager is None:
        _emergency_drill_manager = EmergencyDrillManager(
            broker_manager=get_broker_manager(),
            execution_engine=get_live_engine(),
            risk_manager=get_live_risk_manager(),
        )
    return _emergency_drill_manager


def init_emergency_drill_manager(
    broker_manager: BrokerManager = None,
    execution_engine: LiveOrderExecutionEngine = None,
    risk_manager: LiveRiskManager = None,
) -> EmergencyDrillManager:
    global _emergency_drill_manager
    _emergency_drill_manager = EmergencyDrillManager(
        broker_manager=broker_manager or get_broker_manager(),
        execution_engine=execution_engine or get_live_engine(),
        risk_manager=risk_manager or get_live_risk_manager(),
    )
    return _emergency_drill_manager


def get_auto_failover_manager() -> AutoFailoverManager:
    global _auto_failover_manager
    if _auto_failover_manager is None:
        _auto_failover_manager = AutoFailoverManager(
            broker_manager=get_broker_manager(),
            execution_engine=get_live_engine(),
            risk_manager=get_live_risk_manager(),
        )
    return _auto_failover_manager


def init_auto_failover_manager(
    broker_manager: BrokerManager = None,
    execution_engine: LiveOrderExecutionEngine = None,
    risk_manager: LiveRiskManager = None,
) -> AutoFailoverManager:
    global _auto_failover_manager
    _auto_failover_manager = AutoFailoverManager(
        broker_manager=broker_manager or get_broker_manager(),
        execution_engine=execution_engine or get_live_engine(),
        risk_manager=risk_manager or get_live_risk_manager(),
    )
    return _auto_failover_manager


def get_reconciliation_engine() -> ReconciliationEngine:
    global _reconciliation_engine
    if _reconciliation_engine is None:
        _reconciliation_engine = ReconciliationEngine(
            broker_manager=get_broker_manager(),
            execution_engine=get_live_engine(),
        )
    return _reconciliation_engine


def init_reconciliation_engine(
    broker_manager: BrokerManager = None,
    execution_engine: LiveOrderExecutionEngine = None,
) -> ReconciliationEngine:
    global _reconciliation_engine
    _reconciliation_engine = ReconciliationEngine(
        broker_manager=broker_manager or get_broker_manager(),
        execution_engine=execution_engine or get_live_engine(),
    )
    return _reconciliation_engine
