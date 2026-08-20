"""租户核心数据模型."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Set
from uuid import uuid4

from quant.utils.logger import get_logger

logger = get_logger("tenant.models")


class TenantStatus(Enum):
    """租户状态."""
    ACTIVE = "active"           # 正常运行
    SUSPENDED = "suspended"     # 暂停（配额超限/违规）
    ARCHIVED = "archived"       # 归档（不再使用）
    PROVISIONING = "provisioning"  # 创建中
    DEPROVISIONING = "deprovisioning"  # 删除中


class ResourceType(Enum):
    """资源类型."""
    CPU = "cpu"                    # CPU 核心数
    MEMORY = "memory"              # 内存 (MB)
    STORAGE = "storage"            # 存储 (GB)
    CPU_TIME = "cpu_time"          # CPU 时间 (核小时/天)
    API_CALLS = "api_calls"        # API 调用次数/天
    STORAGE_IO = "storage_io"      # 存储 IOPS
    NETWORK = "network"            # 网络带宽 (Mbps)
    FACTOR_COMPUTE = "factor_compute"  # 因子计算任务数
    DATA_SYNC = "data_sync"        # 数据同步任务数


@dataclass
class ResourceQuota:
    """资源配额."""
    resource_type: ResourceType
    hard_limit: float              # 硬限制（不可超越）
    soft_limit: float              # 软限制（触发告警）
    unit: str                      # 单位
    grace_period_seconds: int = 3600  # 超过软限制后的宽限期

    def is_exceeded(self, usage: float) -> bool:
        return usage > self.hard_limit

    def is_warning(self, usage: float) -> bool:
        return usage > self.soft_limit

    def utilization(self, usage: float) -> float:
        return usage / self.hard_limit if self.hard_limit > 0 else 0.0


@dataclass
class ResourceUsage:
    """资源使用量."""
    resource_type: ResourceType
    current_usage: float
    peak_usage: float = 0.0
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def update(self, usage: float):
        self.current_usage = usage
        if usage > self.peak_usage:
            self.peak_usage = usage
        self.updated_at = datetime.utcnow()


@dataclass
class Tenant:
    """租户实体."""
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    display_name: str = ""
    description: str = ""
    status: TenantStatus = TenantStatus.PROVISIONING
    owner_id: str = ""           # 所有者用户 ID
    admin_ids: List[str] = field(default_factory=list)  # 管理员用户 ID
    member_ids: List[str] = field(default_factory=list)  # 成员用户 ID

    # 资源配额
    quotas: Dict[ResourceType, ResourceQuota] = field(default_factory=dict)

    # 资源使用量
    usages: Dict[ResourceType, ResourceUsage] = field(default_factory=dict)

    # 命名空间前缀（用于数据隔离）
    namespace_prefix: str = ""

    # 元数据
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)

    # 时间戳
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    suspended_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None

    def __post_init__(self):
        if not self.namespace_prefix:
            self.namespace_prefix = f"tenant_{self.id[:8]}"

    def is_active(self) -> bool:
        return self.status == TenantStatus.ACTIVE

    def can_access(self, user_id: str) -> bool:
        return user_id == self.owner_id or user_id in self.admin_ids or user_id in self.member_ids

    def is_admin(self, user_id: str) -> bool:
        return user_id == self.owner_id or user_id in self.admin_ids

    def get_quota(self, resource_type: ResourceType) -> Optional[ResourceQuota]:
        return self.quotas.get(resource_type)

    def get_usage(self, resource_type: ResourceType) -> Optional[ResourceUsage]:
        return self.usages.get(resource_type)

    def check_quota(self, resource_type: ResourceType, requested: float = 1.0) -> tuple[bool, str]:
        """检查配额是否充足."""
        quota = self.quotas.get(resource_type)
        usage = self.usages.get(resource_type)

        if not quota:
            return True, "no quota limit"

        current = usage.current_usage if usage else 0.0
        projected = current + requested

        if projected > quota.hard_limit:
            return False, f"quota exceeded: {projected}/{quota.hard_limit} {quota.unit}"

        if projected > quota.soft_limit:
            return True, f"quota warning: {projected}/{quota.soft_limit} {quota.unit} (soft limit)"

        return True, "ok"

    def record_usage(self, resource_type: ResourceType, usage: float):
        """记录资源使用量."""
        if resource_type not in self.usages:
            self.usages[resource_type] = ResourceUsage(
                resource_type=resource_type,
                current_usage=usage,
            )
        else:
            self.usages[resource_type].update(usage)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name,
            "status": self.status.value,
            "namespace_prefix": self.namespace_prefix,
            "quotas": {rt.value: {"hard": q.hard_limit, "soft": q.soft_limit, "unit": q.unit}
                       for rt, q in self.quotas.items()},
            "usages": {rt.value: {"current": u.current_usage, "peak": u.peak_usage}
                       for rt, u in self.usages.items()},
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }