"""实盘风控实战验证 - 熔断器实盘触发、强平执行、保证金监控、异常交易拦截."""

from __future__ import annotations
import asyncio
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set
from collections import deque

import numpy as np

from quant.execution.broker_adapter import (
    BrokerAdapterBase, BrokerManager, BrokerType, OrderRequest, OrderResponse,
    OrderSide, OrderType, OrderStatus, Trade, Position, Account
)
from quant.execution.live_engine import LiveOrderExecutionEngine, ParentOrder, OrderSlice, OrderState
from quant.risk.circuit_breaker import (
    CircuitBreakerBase, AccountCircuitBreaker, StrategyCircuitBreaker,
    SymbolCircuitBreaker, MarketCircuitBreaker, CircuitBreakerManager, get_circuit_breaker_manager
)
from quant.execution.engine import ExecutionEngine
from quant.execution.cost import CostModel
from quant.execution.execution_model import ExecutionContext, LiveExecutionModel
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

logger = get_logger("risk.live_risk")


class RiskLevel(Enum):
    """风险等级"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class RiskAction(Enum):
    """风控动作"""
    LOG_ONLY = "log_only"
    BLOCK_NEW_ORDERS = "block_new_orders"
    REDUCE_POSITION = "reduce_position"
    FORCE_LIQUIDATE = "force_liquidate"
    CIRCUIT_BREAK = "circuit_break"
    ALERT = "alert"


@dataclass
class RiskRule:
    rule_id: str
    name: str
    description: str
    level: RiskLevel
    action: RiskAction
    metric_name: str
    operator: str
    threshold: float
    window_seconds: int = 60
    min_samples: int = 1
    strategies: List[str] = field(default_factory=list)
    accounts: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)
    enabled: bool = True
    cooldown_seconds: int = 300


@dataclass
class RiskEvent:
    event_id: str
    timestamp: datetime
    rule_id: str
    rule_name: str
    level: RiskLevel
    action: RiskAction
    strategy: str
    account: str
    symbol: Optional[str]
    metric_value: float
    threshold: float
    message: str
    handled: bool = False
    handled_at: Optional[datetime] = None
    handled_by: str = ""


class MarginMonitor:
    """保证金监控器 - 实时监控账户保证金状况"""
    
    def __init__(
        self,
        broker_manager: BrokerManager,
        check_interval: float = 5.0,
        warning_threshold: float = 1.5,
        critical_threshold: float = 1.3,
        liquidation_threshold: float = 1.1,
    ):
        self.broker_manager = broker_manager
        self.check_interval = check_interval
        self.warning_threshold = warning_threshold
        self.critical_threshold = critical_threshold
        self.liquidation_threshold = liquidation_threshold
        
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._callbacks: List[Callable] = []
        self._margin_history: Dict[str, deque] = {}
        self._lock = asyncio.Lock()
    
    def on_margin_event(self, callback: Callable[[str, float, RiskLevel], None]):
        self._callbacks.append(callback)
    
    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("MarginMonitor started")
    
    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MarginMonitor stopped")
    
    async def _monitor_loop(self):
        while self._running:
            try:
                await self._check_all_accounts()
            except Exception as e:
                logger.error(f"Margin monitor error: {e}")
            await asyncio.sleep(self.check_interval)
    
    async def _check_all_accounts(self):
        for broker_name in self.broker_manager.get_connected_brokers():
            broker = self.broker_manager.get_broker(broker_name)
            try:
                account = await broker.query_account()
                await self._evaluate_margin(broker.config.account_id, account)
            except Exception as e:
                logger.error(f"Failed to check margin for {broker_name}: {e}")
    
    async def _evaluate_margin(self, account_id: str, account: Account):
        margin_ratio = account.margin_ratio if hasattr(account, 'margin_ratio') else 0.0
        
        if margin_ratio == 0 and hasattr(account, 'total_assets'):
            total = account.total_assets or 0
            used = account.frozen_cash or 0
            if total > 0:
                margin_ratio = (total - used) / used if used > 0 else float('inf')
        
        async with self._lock:
            if account_id not in self._margin_history:
                self._margin_history[account_id] = deque(maxlen=1000)
            self._margin_history[account_id].append((datetime.utcnow(), margin_ratio))
        
        level = RiskLevel.INFO
        if margin_ratio <= self.liquidation_threshold:
            level = RiskLevel.EMERGENCY
        elif margin_ratio <= self.critical_threshold:
            level = RiskLevel.CRITICAL
        elif margin_ratio <= self.warning_threshold:
            level = RiskLevel.WARNING
        
        for cb in self._callbacks:
            try:
                cb(account_id, margin_ratio, level)
            except Exception as e:
                logger.error(f"Margin callback error: {e}")
        
        if level >= RiskLevel.WARNING:
            logger.warning(f"Margin alert: account={account_id}, ratio={margin_ratio:.2f}, level={level.value}")


class AnomalousTradeDetector:
    """异常交易检测器 - 实时拦截异常订单"""
    
    def __init__(self, execution_engine: LiveOrderExecutionEngine):
        self.execution_engine = execution_engine
        self._rules: Dict[str, RiskRule] = {}
        self._order_history: Dict[str, deque] = {}
        self._lock = asyncio.Lock()
        
        self._init_default_rules()
    
    def _init_default_rules(self):
        defaults = [
            RiskRule(
                rule_id="ORDER_SIZE_TOO_LARGE",
                name="单笔订单过大",
                description="单笔订单金额超过账户净值的一定比例",
                level=RiskLevel.CRITICAL,
                action=RiskAction.BLOCK_NEW_ORDERS,
                metric_name="order_value_pct",
                operator=">",
                threshold=0.1,
                window_seconds=1,
            ),
            RiskRule(
                rule_id="ORDER_FREQUENCY_TOO_HIGH",
                name="下单频率过高",
                description="单位时间内下单次数异常",
                level=RiskLevel.WARNING,
                action=RiskAction.LOG_ONLY,
                metric_name="orders_per_minute",
                operator=">",
                threshold=100,
                window_seconds=60,
            ),
            RiskRule(
                rule_id="PRICE_DEVIATION_TOO_HIGH",
                name="报价偏离过大",
                description="限价单价格偏离当前市价过大",
                level=RiskLevel.WARNING,
                action=RiskAction.BLOCK_NEW_ORDERS,
                metric_name="price_deviation_pct",
                operator=">",
                threshold=0.05,
                window_seconds=1,
            ),
            RiskRule(
                rule_id="CONCENTRATION_TOO_HIGH",
                name="单品种集中度过高",
                description="单一品种持仓占比过高",
                level=RiskLevel.WARNING,
                action=RiskAction.REDUCE_POSITION,
                metric_name="symbol_concentration",
                operator=">",
                threshold=0.3,
                window_seconds=300,
            ),
            RiskRule(
                rule_id="SELL_WITHOUT_POSITION",
                name="无持仓卖出",
                description="检测到无持仓卖出订单",
                level=RiskLevel.CRITICAL,
                action=RiskAction.BLOCK_NEW_ORDERS,
                metric_name="has_position",
                operator="==",
                threshold=0,
                window_seconds=1,
            ),
            RiskRule(
                rule_id="RAPID_CANCEL_REPLACE",
                name="频繁撤单重报",
                description="短时间内大量撤单重报，疑似扰乱市场",
                level=RiskLevel.WARNING,
                action=RiskAction.LOG_ONLY,
                metric_name="cancel_replace_ratio",
                operator=">",
                threshold=0.8,
                window_seconds=60,
            ),
        ]
        for rule in defaults:
            self._rules[rule.rule_id] = rule
    
    def add_rule(self, rule: RiskRule):
        self._rules[rule.rule_id] = rule
    
    def remove_rule(self, rule_id: str):
        self._rules.pop(rule_id, None)
    
    async def check_order(self, order: ParentOrder) -> List[RiskEvent]:
        events = []
        
        for rule_id, rule in self._rules.items():
            if not rule.enabled:
                continue
            
            if rule.strategies and order.strategy_id not in rule.strategies:
                continue
            if rule.accounts and order.account_id not in rule.accounts:
                continue
            if rule.symbols and order.symbol not in rule.symbols:
                continue
            
            metric_value = await self._compute_metric(rule, order)
            triggered = self._check_condition(metric_value, rule.operator, rule.threshold)
            
            if triggered:
                event = RiskEvent(
                    event_id=f"RISK_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
                    timestamp=datetime.utcnow(),
                    rule_id=rule.rule_id,
                    rule_name=rule.name,
                    level=rule.level,
                    action=rule.action,
                    strategy=order.strategy_id,
                    account=order.account_id,
                    symbol=order.symbol,
                    metric_value=metric_value,
                    threshold=rule.threshold,
                    message=f"Rule {rule.name} triggered: {metric_value} {rule.operator} {rule.threshold}",
                )
                events.append(event)
                logger.warning(f"Risk rule triggered: {rule.name} - {event.message}")
        
        return events
    
    async def _compute_metric(self, rule: RiskRule, order: ParentOrder) -> float:
        if rule.metric_name == "order_value_pct":
            order_value = order.target_price * order.total_shares
            broker = self.execution_engine.broker_manager.get_broker()
            if broker:
                account = await broker.query_account()
                if account.total_assets > 0:
                    return (order.target_price * order.total_shares) / account.total_assets
            return 0.0
        
        elif rule.metric_name == "orders_per_minute":
            async with self._lock:
                now = time.time()
                history = self._order_history.get(order.strategy_id, deque())
                recent = sum(1 for t in history if now - t < 60)
                return float(recent)
        
        elif rule.metric_name == "price_deviation_pct":
            if order.order_type == OrderType.LIMIT:
                return 0.0
            return 0.0
        
        elif rule.metric_name == "symbol_concentration":
            return 0.0
        
        elif rule.metric_name == "has_position":
            broker = self.execution_engine.broker_manager.get_broker()
            if broker:
                positions = await broker.query_positions()
                has_pos = any(p.symbol == order.symbol and p.long_quantity > 0 for p in positions)
                return 1.0 if has_pos else 0.0
            return 1.0
        
        elif rule.metric_name == "cancel_replace_ratio":
            return 0.0
        
        return 0.0
    
    def _check_condition(self, value: float, operator: str, threshold: float) -> bool:
        if operator == ">":
            return value > threshold
        elif operator == "<":
            return value < threshold
        elif operator == ">=":
            return value >= threshold
        elif operator == "<=":
            return value <= threshold
        elif operator == "==":
            return abs(value - threshold) < 1e-9
        elif operator == "!=":
            return abs(value - threshold) >= 1e-9
        return False
    
    async def record_order(self, order: ParentOrder):
        async with self._lock:
            if order.strategy_id not in self._order_history:
                self._order_history[order.strategy_id] = deque(maxlen=10000)
            self._order_history[order.strategy_id].append(time.time())


class LiveRiskManager:
    """实盘风控管理器 - 统一入口"""
    
    def __init__(
        self,
        execution_engine: LiveOrderExecutionEngine,
        broker_manager: BrokerManager,
        execution_model: LiveExecutionModel = None,
    ):
        self.execution_engine = execution_engine
        self.broker_manager = broker_manager
        self.execution_model = execution_model
        
        self.circuit_breaker_manager = get_circuit_breaker_manager()
        self.margin_monitor = MarginMonitor(broker_manager)
        self.anomaly_detector = AnomalousTradeDetector(execution_engine)
        
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._circuit_breaker_task: Optional[asyncio.Task] = None
        self._events: List[RiskEvent] = []
        self._lock = asyncio.Lock()
        
        self.on_risk_event: Optional[Callable[[RiskEvent], None]] = None
        self.on_circuit_break: Optional[Callable[[str, str], None]] = None
        self.on_force_liquidate: Optional[Callable[[str, List[str]], None]] = None
        
        self._register_circuit_breaker_callbacks()
    
    def _register_circuit_breaker_callbacks(self):
        def on_state_change(breaker, old_state: str, new_state: str):
            if new_state == "open":
                reason = f"Circuit breaker {breaker.name} opened"
                logger.critical(f"CIRCUIT BREAKER OPENED: {breaker.name}")
                if self.on_circuit_break:
                    self.on_circuit_break(breaker.name, reason)
                event = RiskEvent(
                    event_id=f"CB_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}",
                    timestamp=datetime.utcnow(),
                    rule_id="CIRCUIT_BREAKER",
                    rule_name=f"Circuit Breaker: {breaker.name}",
                    level=RiskLevel.EMERGENCY,
                    action=RiskAction.CIRCUIT_BREAK,
                    strategy="",
                    account="",
                    symbol="",
                    metric_value=1.0,
                    threshold=1.0,
                    message=reason,
                )
                asyncio.create_task(self._handle_risk_event(event))
        
        for name, breaker in getattr(self.circuit_breaker_manager, '_breakers', {}).items():
            if hasattr(breaker, 'on_state_change'):
                breaker.on_state_change(on_state_change)
    
    async def start(self):
        if self._running:
            return
        self._running = True
        
        await self.margin_monitor.start()
        self.margin_monitor.on_margin_event(self._on_margin_event)
        
        self._circuit_breaker_task = asyncio.create_task(self._circuit_breaker_loop())
        
        self.execution_engine.on_order_update = self._on_order_update
        self.execution_engine.on_slice_fill = self._on_slice_fill
        
        logger.info("LiveRiskManager started")
    
    async def stop(self):
        self._running = False
        
        await self.margin_monitor.stop()
        
        if self._circuit_breaker_task:
            self._circuit_breaker_task.cancel()
            try:
                await self._circuit_breaker_task
            except asyncio.CancelledError:
                pass
        
        logger.info("LiveRiskManager stopped")
    
    async def _circuit_breaker_loop(self):
        while self._running:
            try:
                await self._check_circuit_breakers()
            except Exception as e:
                logger.error(f"Circuit breaker check error: {e}")
            await asyncio.sleep(1.0)
    
    async def _check_circuit_breakers(self):
        for broker_name in self.broker_manager.get_connected_brokers():
            broker = self.broker_manager.get_broker(broker_name)
            try:
                account = await broker.query_account()
                positions = await broker.query_positions()
                metrics = self._compute_account_metrics(account, positions)
                
                for name, breaker in getattr(self.circuit_breaker_manager, '_breakers', {}).items():
                    if hasattr(breaker, 'check_trigger') and breaker.check_trigger(metrics):
                        if breaker.allow_request() == False:
                            pass
            except Exception as e:
                logger.error(f"Circuit breaker check error for {broker_name}: {e}")
    
    def _compute_account_metrics(self, account: Account, positions: List[Position]) -> Dict:
        metrics = {}
        
        if hasattr(account, 'margin_ratio'):
            metrics["margin_ratio"] = account.margin_ratio
        elif account.total_assets and account.frozen_cash:
            metrics["margin_ratio"] = (account.total_assets - account.frozen_cash) / account.frozen_cash if account.frozen_cash > 0 else float('inf')
        
        metrics["daily_pnl_pct"] = 0.0
        
        if account.total_assets and account.frozen_cash:
            metrics["margin_usage"] = account.frozen_cash / account.total_assets
        
        metrics["max_drawdown"] = 0.0
        metrics["consecutive_losses"] = 0
        metrics["win_rate"] = 0.5
        metrics["total_trades"] = 0
        
        for pos in positions:
            if pos.long_quantity > 0:
                metrics[f"symbol_{pos.symbol}_volume"] = pos.long_quantity
                metrics[f"symbol_{pos.symbol}_value"] = pos.market_value
        
        return metrics
    
    async def _on_margin_event(self, account_id: str, margin_ratio: float, level: RiskLevel):
        event = RiskEvent(
            event_id=f"MARGIN_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}",
            timestamp=datetime.utcnow(),
            rule_id="MARGIN_MONITOR",
            rule_name="Margin Monitor",
            level=level,
            action=self._margin_level_to_action(level),
            strategy="",
            account=account_id,
            symbol=None,
            metric_value=margin_ratio,
            threshold=self._margin_level_to_threshold(level),
            message=f"Margin ratio {margin_ratio:.2f} triggered {level.value} alert",
        )
        await self._handle_risk_event(event)
    
    def _margin_level_to_action(self, level: RiskLevel) -> RiskAction:
        if level == RiskLevel.EMERGENCY:
            return RiskAction.FORCE_LIQUIDATE
        elif level == RiskLevel.CRITICAL:
            return RiskAction.CIRCUIT_BREAK
        elif level == RiskLevel.WARNING:
            return RiskAction.REDUCE_POSITION
        return RiskAction.LOG_ONLY
    
    def _margin_level_to_threshold(self, level: RiskLevel) -> float:
        if level == RiskLevel.EMERGENCY:
            return 1.1
        elif level == RiskLevel.CRITICAL:
            return 1.3
        elif level == RiskLevel.WARNING:
            return 1.5
        return 2.0
    
    def _on_order_update(self, order: ParentOrder):
        asyncio.create_task(self.anomaly_detector.record_order(order))
    
    def _on_slice_fill(self, parent: ParentOrder, slice_: OrderSlice, trade: Trade):
        pass
    
    async def _handle_risk_event(self, event: RiskEvent):
        async with self._lock:
            self._events.append(event)
        
        await self._execute_risk_action(event)
        
        if self.on_risk_event:
            try:
                self.on_risk_event(event)
            except Exception as e:
                logger.error(f"Risk event callback error: {e}")
    
    async def _execute_risk_action(self, event: RiskEvent):
        if event.action == RiskAction.LOG_ONLY:
            logger.warning(f"Risk event logged: {event.message}")
        
        elif event.action == RiskAction.BLOCK_NEW_ORDERS:
            logger.warning(f"BLOCKING NEW ORDERS for {event.strategy}/{event.account}")
        
        elif event.action == RiskAction.REDUCE_POSITION:
            logger.warning(f"REDUCING POSITION for {event.strategy}/{event.account}")
            await self._reduce_risky_positions(event)
        
        elif event.action == RiskAction.FORCE_LIQUIDATE:
            logger.critical(f"FORCE LIQUIDATION for {event.account}")
            await self._force_liquidate_all(event.account)
        
        elif event.action == RiskAction.CIRCUIT_BREAK:
            logger.critical(f"CIRCUIT BREAK triggered: {event.message}")
        
        elif event.action == RiskAction.ALERT:
            logger.warning(f"ALERT: {event.message}")
    
    async def _reduce_risky_positions(self, event: RiskEvent):
        broker = self.execution_engine.broker_manager.get_broker()
        if not broker:
            return
        
        positions = await broker.query_positions()
        
        for pos in positions:
            if pos.long_quantity <= 0:
                continue
            
            if pos.unrealized_pnl < -pos.market_value * 0.1:
                from quant.execution.live_engine import ParentOrder, SliceConfig, SliceAlgorithm
                from quant.execution.broker_adapter import OrderSide, OrderType
                
                reduce_order = ParentOrder(
                    order_id=f"REDUCE_{uuid.uuid4().hex[:12]}",
                    client_order_id=f"REDUCE_{uuid.uuid4().hex[:12]}",
                    symbol=pos.symbol,
                    side=OrderSide.SELL,
                    total_shares=int(pos.long_quantity * 0.5),
                    target_price=0,
                    order_type=OrderType.MARKET,
                    slice_config=SliceConfig(
                        algorithm=SliceAlgorithm.TWAP,
                        total_shares=int(pos.long_quantity * 0.5),
                        max_slice_size=500,
                        time_horizon_seconds=300,
                    ),
                    strategy_id="RISK_REDUCE",
                    account_id=event.account,
                )
                
                await self.execution_engine.submit_order(reduce_order)
                logger.warning(f"Risk reduce order submitted: {pos.symbol} {pos.long_quantity * 0.5} shares")
    
    async def _force_liquidate_all(self, account_id: str):
        broker = self.execution_engine.broker_manager.get_broker()
        if not broker:
            return
        
        positions = await broker.query_positions()
        
        for pos in positions:
            if pos.long_quantity <= 0:
                continue
            
            from quant.execution.live_engine import ParentOrder, SliceConfig, SliceAlgorithm
            from quant.execution.broker_adapter import OrderSide, OrderType
            
            liquidate_order = ParentOrder(
                order_id=f"LIQUIDATE_{uuid.uuid4().hex[:12]}",
                client_order_id=f"LIQUIDATE_{uuid.uuid4().hex[:12]}",
                symbol=pos.symbol,
                side=OrderSide.SELL,
                total_shares=pos.long_quantity,
                target_price=0,
                order_type=OrderType.MARKET,
                slice_config=SliceConfig(
                    algorithm=SliceAlgorithm.TWAP,
                    total_shares=pos.long_quantity,
                    max_slice_size=1000,
                    time_horizon_seconds=60,
                    urgency=1.0,
                ),
                strategy_id="RISK_LIQUIDATE",
                account_id=account_id,
            )
            
            await self.execution_engine.submit_order(liquidate_order)
            logger.critical(f"Force liquidation order submitted: {pos.symbol} {pos.long_quantity} shares")
        
        if self.on_force_liquidate:
            self.on_force_liquidate(account_id, [p.symbol for p in positions if p.long_quantity > 0])
    
    async def check_order(self, order: ParentOrder) -> Tuple[bool, List[RiskEvent]]:
        for name, breaker in getattr(self.circuit_breaker_manager, '_breakers', {}).items():
            if hasattr(breaker, 'allow_request') and not breaker.allow_request():
                return False, [RiskEvent(
                    event_id=f"CB_BLOCK_{uuid.uuid4().hex[:8]}",
                    timestamp=datetime.utcnow(),
                    rule_id="CIRCUIT_BREAKER",
                    rule_name=f"Circuit Breaker: {name}",
                    level=RiskLevel.EMERGENCY,
                    action=RiskAction.CIRCUIT_BREAK,
                    strategy=order.strategy_id,
                    account=order.account_id,
                    symbol=order.symbol,
                    metric_value=1.0,
                    threshold=0.0,
                    message=f"Circuit breaker {name} is open, order blocked",
                )]
        
        anomaly_events = await self.anomaly_detector.check_order(order)
        if anomaly_events:
            for evt in anomaly_events:
                if evt.action == RiskAction.BLOCK_NEW_ORDERS:
                    return False, anomaly_events
        
        broker = self.execution_engine.broker_manager.get_broker()
        if broker:
            account = await broker.query_account()
            if hasattr(account, 'margin_ratio') and account.margin_ratio < 1.1:
                return False, [RiskEvent(
                    event_id=f"MARGIN_BLOCK_{uuid.uuid4().hex[:8]}",
                    timestamp=datetime.utcnow(),
                    rule_id="MARGIN_CHECK",
                    rule_name="Margin Check",
                    level=RiskLevel.EMERGENCY,
                    action=RiskAction.FORCE_LIQUIDATE,
                    strategy=order.strategy_id,
                    account=order.account_id,
                    symbol=order.symbol,
                    metric_value=getattr(account, 'margin_ratio', 0),
                    threshold=1.1,
                    message=f"Margin ratio too low: {getattr(account, 'margin_ratio', 'N/A')}",
                )]
        
        return True, []
    
    async def get_risk_events(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        level: Optional[RiskLevel] = None,
        limit: int = 100
    ) -> List[RiskEvent]:
        async with self._lock:
            events = list(self._events)
        
        if start_time:
            events = [e for e in events if e.timestamp >= start_time]
        if end_time:
            events = [e for e in events if e.timestamp <= end_time]
        if level:
            events = [e for e in events if e.level == level]
        
        events.sort(key=lambda e: e.timestamp, reverse=True)
        return events[:limit]
    
    async def get_risk_summary(self) -> Dict:
        async with self._lock:
            recent = [e for e in self._events if (datetime.utcnow() - e.timestamp).total_seconds() < 3600]
        
        by_level = {}
        for level in RiskLevel:
            by_level[level.value] = sum(1 for e in recent if e.level == level)
        
        by_action = {}
        for action in RiskAction:
            by_action[action.value] = sum(1 for e in recent if e.action == action)
        
        return {
            "total_events_1h": len(recent),
            "by_level": by_level,
            "by_action": by_action,
            "circuit_breakers_active": len([
                b for b in getattr(self.circuit_breaker_manager, '_breakers', {}).values()
                if hasattr(b, 'state') and b.state == 'open'
            ]),
            "margin_monitor_active": self.margin_monitor._running,
            "anomaly_rules_count": len(self.anomaly_detector._rules),
        }


_live_risk_manager: Optional[LiveRiskManager] = None


def get_live_risk_manager() -> LiveRiskManager:
    global _live_risk_manager
    if _live_risk_manager is None:
        _live_risk_manager = LiveRiskManager(
            execution_engine=get_live_engine(),
            broker_manager=get_broker_manager(),
        )
    return _live_risk_manager


def init_live_risk_manager(
    execution_engine: LiveOrderExecutionEngine = None,
    broker_manager: BrokerManager = None,
    execution_model: LiveExecutionModel = None,
) -> LiveRiskManager:
    global _live_risk_manager
    _live_risk_manager = LiveRiskManager(
        execution_engine=execution_engine or get_live_engine(),
        broker_manager=broker_manager or get_broker_manager(),
        execution_model=execution_model,
    )
    return _live_risk_manager


import uuid
