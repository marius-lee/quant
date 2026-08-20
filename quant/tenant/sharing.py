"""跨租户数据共享 - 受控数据共享、行级权限、审计日志."""

from __future__ import annotations
import threading
import time
import json
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set, Callable, Any
from pathlib import Path

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

from .models import Tenant, TenantStatus
from .registry import get_tenant_registry

logger = get_logger("tenant.sharing")


class SharePermission(Enum):
    """共享权限级别."""
    READ = "read"           # 只读
    WRITE = "write"         # 读写
    ADMIN = "admin"         # 管理权限


class ShareStatus(Enum):
    """共享状态."""
    PENDING = "pending"      # 待审批
    ACTIVE = "active"        # 生效中
    REVOKED = "revoked"      # 已撤销
    EXPIRED = "expired"      # 已过期


@dataclass
class ShareRule:
    """共享规则."""
    share_id: str
    source_tenant_id: str
    target_tenant_id: str
    tables: List[str]                    # 共享的表
    columns: Optional[List[str]] = None  # 共享的列（None=全部）
    permission: SharePermission = SharePermission.READ
    status: ShareStatus = ShareStatus.PENDING
    expires_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    created_by: str = ""
    approved_at: Optional[datetime] = None
    approved_by: str = ""
    filters: Dict[str, Any] = field(default_factory=dict)  # 行级过滤条件
    masking_rules: Dict[str, str] = field(default_factory=dict)  # 脱敏规则


class DataSharingManager:
    """跨租户数据共享管理器."""

    _instance: Optional["DataSharingManager"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self._shares: Dict[str, ShareRule] = {}
        self._lock = threading.RLock()
        self._audit_log: List[dict] = []
        self._max_audit_log = 10000

        # 脱敏函数注册表
        self._masking_functions: Dict[str, Callable[[Any], Any]] = {
            "mask_id": lambda x: f"{str(x)[:3]}***{str(x)[-3:]}" if x else "",
            "mask_phone": lambda x: f"{str(x)[:3]}****{str(x)[-4:]}" if x else "",
            "mask_email": lambda x: f"{x.split('@')[0][:2]}***@{x.split('@')[1]}" if x and '@' in x else "",
            "mask_name": lambda x: f"{x[0]}**" if x else "",
            "hash": lambda x: hashlib.sha256(str(x).encode()).hexdigest()[:8] if x else "",
        }

    def request_share(
        self,
        source_tenant_id: str,
        target_tenant_id: str,
        tables: List[str],
        permission: SharePermission = SharePermission.READ,
        columns: Optional[List[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
        masking_rules: Optional[Dict[str, str]] = None,
        expires_at: Optional[datetime] = None,
        requester_id: str = "",
    ) -> str:
        """请求数据共享."""
        from .registry import get_tenant_registry
        registry = get_tenant_registry()

        source = registry.get_tenant(source_tenant_id)
        target = registry.get_tenant(target_tenant_id)

        if not source or not target:
            raise ValueError("Source or target tenant not found")

        if not source.is_active() or not target.is_active():
            raise ValueError("Source or target tenant not active")

        share_id = f"share_{hashlib.sha256(f'{source_tenant_id}:{target_tenant_id}:{time.time()}'.encode()).hexdigest()[:16]}"

        share = ShareRule(
            share_id=share_id,
            source_tenant_id=source_tenant_id,
            target_tenant_id=target_tenant_id,
            tables=tables,
            columns=columns,
            permission=permission,
            expires_at=expires_at,
            created_by=requester_id,
            filters=filters or {},
            masking_rules=masking_rules or {},
        )

        with self._lock:
            self._shares[share_id] = share
            self._audit_log.append({
                "action": "share_requested",
                "share_id": share_id,
                "source": source_tenant_id,
                "target": target_tenant_id,
                "requester": requester_id,
                "timestamp": datetime.utcnow().isoformat(),
            })

        logger.info(f"Share requested: {share_id} from {source_tenant_id} to {target_tenant_id}")
        return share_id

    def approve_share(self, share_id: str, approver_id: str) -> bool:
        """批准共享请求."""
        with self._lock:
            share = self._shares.get(share_id)
            if not share:
                return False

            if share.status != ShareStatus.PENDING:
                logger.warning(f"Share {share_id} not in pending state: {share.status}")
                return False

            share.status = ShareStatus.ACTIVE
            share.approved_at = datetime.utcnow()
            share.approved_by = approver_id

            self._audit_log.append({
                "action": "share_approved",
                "share_id": share_id,
                "approver": approver_id,
                "timestamp": datetime.utcnow().isoformat(),
            })

            logger.info(f"Share {share_id} approved by {approver_id}")
            return True

    def revoke_share(self, share_id: str, revoker_id: str, reason: str = "") -> bool:
        """撤销共享."""
        with self._lock:
            share = self._shares.get(share_id)
            if not share:
                return False

            share.status = ShareStatus.REVOKED
            self._audit_log.append({
                "action": "share_revoked",
                "share_id": share_id,
                "revoker": revoker_id,
                "reason": reason,
                "timestamp": datetime.utcnow().isoformat(),
            })

            logger.info(f"Share {share_id} revoked by {revoker_id}: {reason}")
            return True

    def get_share(self, share_id: str) -> Optional[ShareRule]:
        return self._shares.get(share_id)

    def list_shares(
        self,
        source_tenant_id: Optional[str] = None,
        target_tenant_id: Optional[str] = None,
        status: Optional[ShareStatus] = None,
    ) -> List[ShareRule]:
        with self._lock:
            shares = list(self._shares.values())
            if source_tenant_id:
                shares = [s for s in shares if s.source_tenant_id == source_tenant_id]
            if target_tenant_id:
                shares = [s for s in shares if s.target_tenant_id == target_tenant_id]
            if status:
                shares = [s for s in shares if s.status == status]
            return shares

    def apply_masking(self, share_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
        """应用脱敏规则."""
        share = self._shares.get(share_id)
        if not share or share.status != ShareStatus.ACTIVE:
            return row

        masked = row.copy()
        for col, rule_name in share.masking_rules.items():
            if col in masked:
                func = self._masking_functions.get(rule_name)
                if func:
                    masked[col] = func(masked[col])
        return masked

    def filter_row(self, share_id: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """应用行级过滤."""
        share = self._shares.get(share_id)
        if not share or share.status != ShareStatus.ACTIVE:
            return None

        # 检查过滤条件
        for col, condition in share.filters.items():
            if col not in row:
                return None
            if not self._eval_condition(row[col], condition):
                return None

        # 应用列选择
        if share.columns:
            filtered = {k: v for k, v in row.items() if k in share.columns}
        else:
            filtered = row

        # 应用脱敏
        return self.apply_masking(share_id, filtered)

    def _eval_condition(self, value: Any, condition: Any) -> bool:
        """评估过滤条件."""
        if isinstance(condition, dict):
            op = condition.get("op", "eq")
            val = condition.get("value")
            if op == "eq":
                return value == val
            elif op == "neq":
                return value != val
            elif op == "gt":
                return value > val
            elif op == "gte":
                return value >= val
            elif op == "lt":
                return value < val
            elif op == "lte":
                return value <= val
            elif op == "in":
                return value in val
            elif op == "contains":
                return val in str(value)
        return value == condition

    def get_audit_log(self, limit: int = 100) -> List[dict]:
        return self._audit_log[-limit:]

    def register_masking_function(self, name: str, func: Callable[[Any], Any]):
        """注册自定义脱敏函数."""
        self._masking_functions[name] = func


# 全局实例
_data_sharing_manager: Optional[DataSharingManager] = None


def get_data_sharing_manager() -> DataSharingManager:
    global _data_sharing_manager
    if _data_sharing_manager is None:
        _data_sharing_manager = DataSharingManager()
    return _data_sharing_manager