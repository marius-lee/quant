"""监管合规与审计 - 客户适当性、穿透式监管报送、异常交易监测、资金账户管理."""

from __future__ import annotations
import asyncio
import csv
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from collections import defaultdict, deque
from pathlib import Path

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

logger = get_logger("compliance")


class ClientRiskLevel(Enum):
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"
    VERY_AGGRESSIVE = "very_aggressive"
    UNKNOWN = "unknown"


class ProductRiskLevel(Enum):
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    R4 = "R4"
    R5 = "R5"


class ReportType(Enum):
    DAILY_TRADE = "daily_trade"
    POSITION_SNAPSHOT = "position_snapshot"
    FUND_FLOW = "fund_flow"
    LARGE_TRADE = "large_trade"
    ABNORMAL_TRADE = "abnormal_trade"
    CLIENT_INFO = "client_info"
    RISK_INDICATOR = "risk_indicator"
    MONTHLY_SUMMARY = "monthly_summary"


class FundAccountType(Enum):
    MARGIN = "margin"
    CASH = "cash"
    CREDIT = "credit"
    DERIVATIVES = "derivatives"
    THIRD_PARTY = "third_party"


@dataclass
class ClientProfile:
    client_id: str
    name: str
    id_type: str = "ID_CARD"
    id_number: str = ""
    phone: str = ""
    email: str = ""
    risk_level: ClientRiskLevel = ClientRiskLevel.UNKNOWN
    assessment_date: Optional[date] = None
    assessment_version: str = "1.0"
    assessment_score: int = 0
    is_professional: bool = False
    is_qualified_investor: bool = False
    financial_assets: float = 0.0
    investment_experience_years: int = 0
    account_ids: List[str] = field(default_factory=list)
    fund_account_type: FundAccountType = FundAccountType.CASH
    status: str = "active"
    blacklisted: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ProductInfo:
    product_code: str
    name: str
    product_type: str
    risk_level: ProductRiskLevel
    issuer: str = ""
    manager: str = ""
    min_investment: float = 0.0
    suitable_clients: List[ClientRiskLevel] = field(default_factory=list)
    restricted_clients: List[str] = field(default_factory=list)
    status: str = "active"
    launch_date: Optional[date] = None
    expiry_date: Optional[date] = None


@dataclass
class SuitabilityCheckResult:
    client_id: str
    product_code: str
    passed: bool
    reason: str = ""
    risk_match: bool = True
    qualification_match: bool = True
    warnings: List[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class RegulatoryReport:
    report_id: str
    report_type: ReportType
    reporting_date: date
    data: Dict[str, Any]
    file_path: str = ""
    file_hash: str = ""
    status: str = "pending"
    submitted_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    error_message: str = ""
    retry_count: int = 0


@dataclass
class FundAccount:
    account_id: str
    client_id: str
    account_type: FundAccountType
    broker_name: str
    total_assets: float = 0.0
    available_cash: float = 0.0
    frozen_cash: float = 0.0
    market_value: float = 0.0
    margin_ratio: float = 0.0
    margin_used: float = 0.0
    margin_available: float = 0.0
    status: str = "active"
    currency: str = "CNY"
    custodian: str = ""
    custody_account: str = ""
    last_sync: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class AuditLogEntry:
    log_id: str
    timestamp: datetime
    event_type: str
    operator: str
    operator_type: str
    action: str
    resource_type: str
    resource_id: str
    before: Dict[str, Any] = field(default_factory=dict)
    after: Dict[str, Any] = field(default_factory=dict)
    ip_address: str = ""
    user_agent: str = ""
    session_id: str = ""
    result: str = "success"
    error_message: str = ""
    severity: str = "info"


class ClientSuitabilityManager:
    def __init__(self):
        self._clients: Dict[str, ClientProfile] = {}
        self._products: Dict[str, ProductInfo] = {}
        self._check_history: List[SuitabilityCheckResult] = []
        self._lock = asyncio.Lock()

    def register_client(self, client: ClientProfile) -> bool:
        if client.client_id in self._clients:
            return False
        self._clients[client.client_id] = client
        return True

    def update_client(self, client_id: str, **kwargs) -> bool:
        if client_id not in self._clients:
            return False
        client = self._clients[client_id]
        for k, v in kwargs.items():
            if hasattr(client, k):
                setattr(client, k, v)
        client.updated_at = datetime.utcnow()
        return True

    def register_product(self, product: ProductInfo) -> bool:
        if product.product_code in self._products:
            return False
        self._products[product.product_code] = product
        return True

    async def check_suitability(
        self,
        client_id: str,
        product_code: str,
        investment_amount: float = 0.0,
    ) -> SuitabilityCheckResult:
        async with self._lock:
            client = self._clients.get(client_id)
            product = self._products.get(product_code)

            if not client:
                return SuitabilityCheckResult(
                    client_id=client_id,
                    product_code=product_code,
                    passed=False,
                    reason="Client not found",
                )

            if not product:
                return SuitabilityCheckResult(
                    client_id=client_id,
                    product_code=product_code,
                    passed=False,
                    reason="Product not found",
                )

            warnings = []
            risk_match = True
            qualification_match = True

            risk_order = {
                ClientRiskLevel.CONSERVATIVE: 1,
                ClientRiskLevel.MODERATE: 2,
                ClientRiskLevel.AGGRESSIVE: 3,
                ClientRiskLevel.VERY_AGGRESSIVE: 4,
            }
            product_risk_order = {
                ProductRiskLevel.R1: 1,
                ProductRiskLevel.R2: 2,
                ProductRiskLevel.R3: 3,
                ProductRiskLevel.R4: 4,
                ProductRiskLevel.R5: 5,
            }

            if risk_order.get(client.risk_level, 0) < product_risk_order.get(product.risk_level, 0):
                risk_match = False
                warnings.append(f"Client risk level {client.risk_level.value} lower than product risk {product.risk_level.value}")

            if product.min_investment > 0 and investment_amount < product.min_investment:
                qualification_match = False
                warnings.append(f"Investment amount {investment_amount} below minimum {product.min_investment}")

            if client.blacklisted:
                qualification_match = False
                warnings.append("Client is blacklisted")

            if client.status != "active":
                qualification_match = False
                warnings.append(f"Client status is {client.status}")

            if product.risk_level in (ProductRiskLevel.R4, ProductRiskLevel.R5):
                if not (client.is_professional or client.is_qualified_investor):
                    qualification_match = False
                    warnings.append("High-risk product requires professional/qualified investor status")

            if client.client_id in product.restricted_clients:
                qualification_match = False
                warnings.append("Client is restricted from this product")

            passed = risk_match and qualification_match
            reason = "" if passed else "; ".join(warnings)

            result = SuitabilityCheckResult(
                client_id=client_id,
                product_code=product_code,
                passed=passed,
                reason=reason,
                risk_match=risk_match,
                qualification_match=qualification_match,
                warnings=warnings,
            )

            self._check_history.append(result)
            return result

    def get_client(self, client_id: str) -> Optional[ClientProfile]:
        return self._clients.get(client_id)

    def get_product(self, product_code: str) -> Optional[ProductInfo]:
        return self._products.get(product_code)

    def get_check_history(
        self,
        client_id: Optional[str] = None,
        product_code: Optional[str] = None,
        limit: int = 100,
    ) -> List[SuitabilityCheckResult]:
        history = self._check_history
        if client_id:
            history = [h for h in history if h.client_id == client_id]
        if product_code:
            history = [h for h in history if h.product_code == product_code]
        return history[-limit:]


class RegulatoryReportingEngine:
    def __init__(
        self,
        broker_manager: BrokerManager,
        execution_engine: LiveOrderExecutionEngine,
        risk_manager: LiveRiskManager,
        output_dir: str = "compliance_reports",
    ):
        self.broker_manager = broker_manager
        self.execution_engine = execution_engine
        self.risk_manager = risk_manager
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._running = False
        self._report_task: Optional[asyncio.Task] = None
        self._reports: Dict[str, RegulatoryReport] = {}
        self._lock = asyncio.Lock()

        self.report_schedules: Dict[ReportType, Dict] = {
            ReportType.DAILY_TRADE: {"cron": "0 18 * * 1-5", "enabled": True},
            ReportType.POSITION_SNAPSHOT: {"cron": "0 16 * * 1-5", "enabled": True},
            ReportType.FUND_FLOW: {"cron": "0 18 * * 1-5", "enabled": True},
            ReportType.LARGE_TRADE: {"cron": "*/30 * * * 1-5", "enabled": True},
            ReportType.ABNORMAL_TRADE: {"cron": "*/15 * * * 1-5", "enabled": True},
            ReportType.RISK_INDICATOR: {"cron": "0 18 * * 1-5", "enabled": True},
            ReportType.MONTHLY_SUMMARY: {"cron": "0 9 1 * *", "enabled": True},
        }

        self.large_trade_threshold = 10000000.0
        self.abnormal_price_deviation = 0.09
        self.abnormal_volume_multiple = 5.0

    async def start(self):
        if self._running:
            return
        self._running = True
        self._report_task = asyncio.create_task(self._report_loop())
        logger.info("RegulatoryReportingEngine started")

    async def stop(self):
        self._running = False
        if self._report_task:
            self._report_task.cancel()
            try:
                await self._report_task
            except asyncio.CancelledError:
                pass
        logger.info("RegulatoryReportingEngine stopped")

    async def _report_loop(self):
        while self._running:
            try:
                now = datetime.utcnow()
                for report_type, schedule in self.report_schedules.items():
                    if not schedule.get("enabled", True):
                        continue
                    if self._should_run_report(schedule["cron"], now):
                        await self.generate_report(report_type, now.date())
            except Exception as e:
                logger.error(f"Report loop error: {e}")
            await asyncio.sleep(60)

    def _should_run_report(self, cron_expr: str, now: datetime) -> bool:
        if cron_expr == "0 18 * * 1-5":
            return now.hour == 18 and now.minute == 0 and now.weekday() < 5
        if cron_expr == "0 16 * * 1-5":
            return now.hour == 16 and now.minute == 0 and now.weekday() < 5
        if cron_expr == "*/30 * * * 1-5":
            return now.minute % 30 == 0 and now.weekday() < 5
        if cron_expr == "*/15 * * * 1-5":
            return now.minute % 15 == 0 and now.weekday() < 5
        if cron_expr == "0 9 1 * *":
            return now.day == 1 and now.hour == 9 and now.minute == 0
        return False

    async def generate_report(
        self,
        report_type: ReportType,
        reporting_date: date,
    ) -> RegulatoryReport:
        report_id = f"{report_type.value}_{reporting_date}_{uuid.uuid4().hex[:8]}"

        data = await self._collect_report_data(report_type, reporting_date)

        filename = f"{report_id}.json"
        file_path = self.output_dir / filename

        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        with open(file_path, 'rb') as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()

        report = RegulatoryReport(
            report_id=report_id,
            report_type=report_type,
            reporting_date=reporting_date,
            data=data,
            file_path=str(file_path),
            file_hash=file_hash,
            status="generated",
        )

        async with self._lock:
            self._reports[report_id] = report

        logger.info(f"Generated report: {report_id}")
        return report

    async def _collect_report_data(
        self,
        report_type: ReportType,
        reporting_date: date,
    ) -> Dict[str, Any]:
        base_data = {
            "report_type": report_type.value,
            "reporting_date": reporting_date.isoformat(),
            "generated_at": datetime.utcnow().isoformat(),
            "system_version": "1.0",
        }

        if report_type == ReportType.DAILY_TRADE:
            trades_summary = {}
            for broker_name in self.broker_manager.get_connected_brokers():
                broker = self.broker_manager.get_broker(broker_name)
                trades = await broker.query_trades()
                trades_summary[broker_name] = {
                    "trade_count": len(trades),
                    "total_volume": sum(t.quantity for t in trades),
                    "total_amount": sum(t.price * t.quantity for t in trades),
                    "buy_count": sum(1 for t in trades if t.side == OrderSide.BUY),
                    "sell_count": sum(1 for t in trades if t.side == OrderSide.SELL),
                }
            base_data["trades_summary"] = trades_summary

        elif report_type == ReportType.POSITION_SNAPSHOT:
            positions_summary = {}
            for broker_name in self.broker_manager.get_connected_brokers():
                broker = self.broker_manager.get_broker(broker_name)
                positions = await broker.query_positions()
                positions_summary[broker_name] = [
                    {
                        "symbol": p.symbol,
                        "long_quantity": p.long_quantity,
                        "short_quantity": p.short_quantity,
                        "market_value": p.market_value,
                        "unrealized_pnl": p.unrealized_pnl,
                    }
                    for p in positions
                ]
            base_data["positions"] = positions_summary

        elif report_type == ReportType.FUND_FLOW:
            fund_flows = {}
            for broker_name in self.broker_manager.get_connected_brokers():
                broker = self.broker_manager.get_broker(broker_name)
                account = await broker.query_account()
                fund_flows[broker_name] = {
                    "total_assets": account.total_assets,
                    "available_cash": account.available_cash,
                    "frozen_cash": account.frozen_cash,
                    "margin_ratio": getattr(account, 'margin_ratio', 0.0),
                }
            base_data["fund_flows"] = fund_flows

        elif report_type == ReportType.LARGE_TRADE:
            large_trades = []
            for broker_name in self.broker_manager.get_connected_brokers():
                broker = self.broker_manager.get_broker(broker_name)
                trades = await broker.query_trades()
                for t in trades:
                    amount = t.price * t.quantity
                    if amount >= self.large_trade_threshold:
                        large_trades.append({
                            "trade_id": t.trade_id,
                            "symbol": t.symbol,
                            "side": t.side.value,
                            "price": t.price,
                            "quantity": t.quantity,
                            "amount": amount,
                            "timestamp": t.timestamp.isoformat(),
                            "broker": broker_name,
                        })
            base_data["large_trades"] = large_trades

        elif report_type == ReportType.ABNORMAL_TRADE:
            abnormal_trades = []
            for broker_name in self.broker_manager.get_connected_brokers():
                broker = self.broker_manager.get_broker(broker_name)
                trades = await broker.query_trades()
                for t in trades:
                    if abs(t.price - t.price) / t.price > self.abnormal_price_deviation:
                        abnormal_trades.append({
                            "type": "price_deviation",
                            "trade_id": t.trade_id,
                            "symbol": t.symbol,
                            "deviation_pct": (t.price - t.price) / t.price,
                        })
            base_data["abnormal_trades"] = abnormal_trades

        elif report_type == ReportType.RISK_INDICATOR:
            risk_summary = {}
            for broker_name in self.broker_manager.get_connected_brokers():
                broker = self.broker_manager.get_broker(broker_name)
                account = await broker.query_account()
                positions = await broker.query_positions()

                risk_summary[broker_name] = {
                    "margin_ratio": getattr(account, 'margin_ratio', 0.0),
                    "total_exposure": sum(p.market_value for p in positions),
                    "max_single_position": max((p.market_value for p in positions), default=0),
                    "concentration_pct": max((p.market_value for p in positions), default=0) / account.total_assets if account.total_assets > 0 else 0,
                    "unrealized_pnl": sum(p.unrealized_pnl for p in positions),
                }
            base_data["risk_indicators"] = risk_summary

        return base_data

    async def submit_report(self, report_id: str) -> bool:
        async with self._lock:
            report = self._reports.get(report_id)
            if not report:
                return False

            report.status = "submitted"
            report.submitted_at = datetime.utcnow()

            await asyncio.sleep(0.1)
            report.status = "accepted"
            report.accepted_at = datetime.utcnow()

            return True

    def get_report(self, report_id: str) -> Optional[RegulatoryReport]:
        return self._reports.get(report_id)

    def list_reports(
        self,
        report_type: Optional[ReportType] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[RegulatoryReport]:
        reports = list(self._reports.values())
        if report_type:
            reports = [r for r in reports if r.report_type == report_type]
        if start_date:
            reports = [r for r in reports if r.reporting_date >= start_date]
        if end_date:
            reports = [r for r in reports if r.reporting_date <= end_date]
        return sorted(reports, key=lambda r: r.reporting_date, reverse=True)


class FundAccountManager:
    def __init__(self, broker_manager: BrokerManager):
        self.broker_manager = broker_manager
        self._accounts: Dict[str, FundAccount] = {}
        self._lock = asyncio.Lock()

    def register_account(self, account: FundAccount) -> bool:
        if account.account_id in self._accounts:
            return False
        self._accounts[account.account_id] = account
        return True

    async def sync_account(self, account_id: str) -> bool:
        async with self._lock:
            account = self._accounts.get(account_id)
            if not account:
                return False

            broker = self.broker_manager.get_broker(account.broker_name)
            if not broker:
                return False

            broker_account = await broker.query_account()

            account.total_assets = broker_account.total_assets
            account.available_cash = broker_account.available_cash
            account.frozen_cash = broker_account.frozen_cash
            account.market_value = broker_account.market_value
            if hasattr(broker_account, 'margin_ratio'):
                account.margin_ratio = broker_account.margin_ratio
            account.last_sync = datetime.utcnow()
            account.updated_at = datetime.utcnow()

            return True

    async def sync_all(self) -> Dict[str, bool]:
        results = {}
        for account_id in self._accounts:
            results[account_id] = await self.sync_account(account_id)
        return results

    def get_account(self, account_id: str) -> Optional[FundAccount]:
        return self._accounts.get(account_id)

    def list_accounts(self, client_id: Optional[str] = None) -> List[FundAccount]:
        accounts = list(self._accounts.values())
        if client_id:
            accounts = [a for a in accounts if a.client_id == client_id]
        return accounts

    def get_total_assets(self) -> float:
        return sum(a.total_assets for a in self._accounts.values())


class AuditTrail:
    def __init__(self, log_dir: str = "audit_logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._current_file: Optional[Path] = None
        self._file_handle = None
        self._lock = asyncio.Lock()
        self._buffer: List[AuditLogEntry] = []
        self._flush_interval = 1.0
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        await self._rotate_log_file()

    async def stop(self):
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_buffer()
        if self._file_handle:
            self._file_handle.close()

    async def _rotate_log_file(self):
        if self._file_handle:
            self._file_handle.close()
        date_str = datetime.utcnow().strftime("%Y%m%d")
        self._current_file = self.log_dir / f"audit_{date_str}.jsonl"
        self._file_handle = open(self._current_file, 'a', encoding='utf-8')

    async def _flush_loop(self):
        while self._running:
            await asyncio.sleep(self._flush_interval)
            await self._flush_buffer()
            if datetime.utcnow().hour == 0 and datetime.utcnow().minute < 5:
                await self._rotate_log_file()

    async def _flush_buffer(self):
        if not self._buffer:
            return
        async with self._lock:
            if not self._file_handle:
                await self._rotate_log_file()
            for entry in self._buffer:
                line = json.dumps(entry.__dict__, ensure_ascii=False, default=str)
                self._file_handle.write(line + '\n')
            self._file_handle.flush()
            self._buffer.clear()

    async def log(self, entry: AuditLogEntry):
        async with self._lock:
            self._buffer.append(entry)

    async def log_event(
        self,
        event_type: str,
        operator: str,
        operator_type: str,
        action: str,
        resource_type: str,
        resource_id: str,
        before: Dict = None,
        after: Dict = None,
        ip_address: str = "",
        user_agent: str = "",
        session_id: str = "",
        result: str = "success",
        error_message: str = "",
        severity: str = "info",
    ):
        entry = AuditLogEntry(
            log_id=f"AUDIT_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
            timestamp=datetime.utcnow(),
            event_type=event_type,
            operator=operator,
            operator_type=operator_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            before=before or {},
            after=after or {},
            ip_address=ip_address,
            user_agent=user_agent,
            session_id=session_id,
            result=result,
            error_message=error_message,
            severity=severity,
        )
        await self.log(entry)

    def verify_integrity(self, date_str: str) -> Tuple[bool, List[str]]:
        log_file = self.log_dir / f"audit_{date_str}.jsonl"
        if not log_file.exists():
            return True, []

        errors = []
        prev_hash = "0" * 64
        with open(log_file, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                try:
                    entry = json.loads(line.strip())
                    content = json.dumps(entry, sort_keys=True, ensure_ascii=False)
                    curr_hash = hashlib.sha256((prev_hash + content).encode()).hexdigest()
                    if entry.get("log_hash") != curr_hash:
                        errors.append(f"Line {line_num}: hash mismatch")
                    prev_hash = curr_hash
                except json.JSONDecodeError:
                    errors.append(f"Line {line_num}: JSON decode error")

        return len(errors) == 0, errors


class ComplianceManager:
    def __init__(
        self,
        broker_manager: BrokerManager,
        execution_engine: LiveOrderExecutionEngine,
        risk_manager: LiveRiskManager,
    ):
        self.broker_manager = broker_manager
        self.execution_engine = execution_engine
        self.risk_manager = risk_manager

        self.suitability = ClientSuitabilityManager()
        self.reporting = RegulatoryReportingEngine(
            broker_manager, execution_engine, risk_manager
        )
        self.fund_accounts = FundAccountManager(broker_manager)
        self.audit_trail = AuditTrail()

        self._running = False

        self.on_violation: Optional[Callable[[str, Dict], None]] = None

    async def start(self):
        if self._running:
            return
        self._running = True

        await self.reporting.start()
        await self.audit_trail.start()

        self._sync_task = asyncio.create_task(self._sync_loop())

        logger.info("ComplianceManager started")

    async def stop(self):
        self._running = False

        await self.reporting.stop()
        await self.audit_trail.stop()

        if hasattr(self, '_sync_task'):
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

        logger.info("ComplianceManager stopped")

    async def _sync_loop(self):
        while self._running:
            try:
                await self.fund_accounts.sync_all()
            except Exception as e:
                logger.error(f"Fund account sync error: {e}")
            await asyncio.sleep(60)

    async def check_order_compliance(
        self,
        order: ParentOrder,
        client_id: str,
    ) -> Tuple[bool, List[str]]:
        violations = []

        product = self.suitability.get_product(order.symbol)
        if product:
            check = await self.suitability.check_suitability(
                client_id, order.symbol, order.target_price * order.total_shares
            )
            if not check.passed:
                violations.append(f"Suitability check failed: {check.reason}")

        client = self.suitability.get_client(client_id)
        if not client:
            violations.append("Client not registered")
        elif client.status != "active":
            violations.append(f"Client status: {client.status}")
        elif client.blacklisted:
            violations.append("Client is blacklisted")

        accounts = self.fund_accounts.list_accounts(client_id)
        active_accounts = [a for a in accounts if a.status == "active"]
        if not active_accounts:
            violations.append("No active fund account")

        return len(violations) == 0, violations

    async def log_audit(self, entry: AuditLogEntry):
        await self.audit_trail.log(entry)

    async def get_compliance_status(self) -> Dict:
        return {
            "suitability_checks_24h": len([
                h for h in self.suitability._check_history
                if (datetime.utcnow() - h.checked_at).total_seconds() < 86400
            ]),
            "reports_generated_24h": len([
                r for r in self.reporting._reports.values()
                if (datetime.utcnow() - r.submitted_at).total_seconds() < 86400
                if r.submitted_at
            ]) if self.reporting._reports else 0,
            "fund_accounts_synced": len(self.fund_accounts._accounts),
            "audit_logs_1h": len(self.audit_trail._buffer),
            "violations_24h": 0,
        }


_compliance_manager: Optional[ComplianceManager] = None


def get_compliance_manager() -> ComplianceManager:
    global _compliance_manager
    if _compliance_manager is None:
        _compliance_manager = ComplianceManager(
            broker_manager=get_broker_manager(),
            execution_engine=get_live_engine(),
            risk_manager=get_live_risk_manager(),
        )
    return _compliance_manager


def init_compliance_manager(
    broker_manager: BrokerManager = None,
    execution_engine: LiveOrderExecutionEngine = None,
    risk_manager: LiveRiskManager = None,
) -> ComplianceManager:
    global _compliance_manager
    _compliance_manager = ComplianceManager(
        broker_manager=broker_manager or get_broker_manager(),
        execution_engine=execution_engine or get_live_engine(),
        risk_manager=risk_manager or get_live_risk_manager(),
    )
    return _compliance_manager
