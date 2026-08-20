"""配额管理器 - 资源配额监控、告警、执行."""

from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable, Set
from collections import defaultdict

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

from .models import Tenant, ResourceType, ResourceQuota, ResourceUsage, TenantStatus

logger = get_logger("tenant.quota")


@dataclass
class QuotaAlert:
    """配额告警."""
    tenant_id: str
    tenant_name: str
    resource_type: str
    alert_type: str  # WARNING, EXCEEDED, GRACE_PERIOD
    current_usage: float
    limit: float
    threshold: float
    message: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


class QuotaManager:
    """配额管理器 - 监控、告警、执行."""

    _instance: Optional["QuotaManager"] = None
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

        from .registry import get_tenant_registry
        self._registry = get_tenant_registry()

        self._alert_handlers: List[Callable[[dict], None]] = []
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

        # 告警历史
        self._alert_history: List[dict] = []
        self._max_alert_history = 10000

        # 配置
        self._check_interval = 30  # 秒
        self._grace_period_default = 3600  # 1小时

    def register_alert_handler(self, handler: Callable[[dict], None]):
        """注册告警处理器."""
        self._alert_handlers.append(handler)

    def check_tenant_quota(self, tenant_id: str) -> Dict[str, dict]:
        """检查单租户配额，返回告警信息."""
        from .registry import get_tenant_registry
        registry = get_tenant_registry()
        tenant = registry.get_tenant(tenant_id)

        if not tenant or not tenant.is_active():
            return {}

        alerts = {}
        for resource_type in ResourceType:
            quota = tenant.get_quota(resource_type)
            usage = tenant.get_usage(resource_type)

            if not quota or not usage:
                continue

            current = usage.current_usage
            hard = quota.hard_limit
            soft = quota.soft_limit

            # 计算使用率
            utilization = current / quota.hard_limit if quota.hard_limit > 0 else 0

            # 检查硬限制
            if current > quota.hard_limit:
                yield {
                    "type": "QUOTA_EXCEEDED",
                    "severity": "critical",
                    "resource": resource_type.value,
                    "current": current,
                    "hard_limit": quota.hard_limit,
                    "soft_limit": quota.soft_limit,
                    "utilization": utilization,
                    "message": f"Quota exceeded: {current}/{quota.hard_limit} {quota.unit}",
                    "grace_period_remaining": quota.grace_period_seconds,
                }

            # 检查软限制
            elif current > quota.soft_limit:
                yield {
                    "type": "QUOTA_WARNING",
                    "severity": "warning",
                    "resource": resource_type.value,
                    "current": current,
                    "hard_limit": quota.hard_limit,
                    "soft_limit": quota.soft_limit,
                    "utilization": utilization,
                    "message": f"Quota warning: {current}/{quota.soft_limit} {quota.unit}",
                }

            # 高使用率预警 (80%)
            elif utilization > 0.8:
                yield {
                    "type": "QUOTA_HIGH_USAGE",
                    "severity": "warning",
                    "resource": resource_type.value,
                    "current": current,
                    "hard_limit": quota.hard_limit,
                    "soft_limit": quota.soft_limit,
                    "utilization": utilization,
                    "message": f"High usage: {utilization:.1%} of quota",
                }

    def check_all_quotas(self) -> Dict[str, List[dict]]:
        """检查所有租户配额."""
        from .registry import get_tenant_registry
        registry = get_tenant_registry()

        all_alerts = {}
        for tenant_id, tenant in registry.get_enabled().items():
            alerts = list(self.check_tenant_quota(tenant_id))
            if alerts:
                # 过滤重复（同一资源只保留最高级别）
                seen = set()
                filtered = []
                for alert in alerts:
                    key = (alert["resource"], alert["type"])
                    if key not in seen:
                        seen.add(key)
                        filtered.append(alert)
                if filtered:
                    all_alerts[tenant_id] = filtered
        return all_alerts

    def record_usage(self, tenant_id: str, resource_type: ResourceType, usage: float):
        """记录资源使用量."""
        from .registry import get_tenant_registry
        registry = get_tenant_registry()
        tenant = registry.get_tenant(tenant_id)
        if tenant:
            tenant.record_usage(resource_type, usage)

    def check_and_alert(self, tenant_id: str) -> List[dict]:
        """检查并触发告警."""
        alerts = list(self.check_tenant_quota(tenant_id))
        for alert in alerts:
            alert["tenant_id"] = tenant_id
            alert["timestamp"] = datetime.utcnow().isoformat()
            self._fire_alert(alert)
        return list(self.check_tenant_quota(tenant_id))

    def _fire_alert(self, alert: dict):
        """触发告警."""
        alert_key = f"{alert['tenant_id']}:{alert['resource']}:{alert['type']}"
        now = time.time()

        # 检查冷却期
        if hasattr(self, "_last_alert_time") and alert in getattr(self, "_last_alert_time", {}):
            if time.time() - self._last_alert_time[alert] < 300:  # 5分钟冷却
                return

        if not hasattr(self, "_last_alert_time"):
            self._last_alert_time = {}
        self._last_alert_time[alert] = time.time()

        # 记录历史
        alert["timestamp"] = datetime.utcnow().isoformat()
        self._alert_history.append(alert)
        if len(self._alert_history) > self._max_alert_history:
            self._alert_history = self._alert_history[-self._max_alert_history:]

        # 调用处理器
        for handler in self._alert_handlers:
            try:
                handler(alert)
            except Exception as e:
                logger.error(f"Alert handler error: {e}")

    def register_alert_handler(self, handler: Callable[[dict], None]):
        """注册告警处理器."""
        self._alert_handlers.append(handler)

    def get_alert_history(
        self,
        tenant_id: Optional[str] = None,
        severity: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100
    ) -> List[dict]:
        """获取告警历史."""
        alerts = self._alert_history
        if tenant_id:
            alerts = [a for a in alerts if a.get("tenant_id") == tenant_id]
        if severity:
            alerts = [a for a in alerts if a.get("severity") == severity]
        if since:
            alerts = [a for a in alerts if datetime.fromisoformat(a["timestamp"]) >= since]
        return alerts[-limit:]

    # ══════════════════════════════════════════════════════════════════
    # 后台监控
    # ══════════════════════════════════════════════════════════════════

    def start_monitoring(self, interval: int = 30):
        """启动配额监控后台任务."""
        if self._running:
            return
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(interval,),
            daemon=True,
            name="quota-monitor"
        )
        self._monitor_thread.start()
        logger.info("Quota monitoring started")

    def stop_monitoring(self):
        """停止监控."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=10)

    def _monitor_loop(self, interval: int):
        while self._running:
            try:
                self.check_all_quotas()
            except Exception as e:
                logger.error(f"Quota monitoring error: {e}")
            time.sleep(interval)

    def shutdown(self):
        """关闭配额管理器."""
        self.stop_monitoring()
        logger.info("Quota manager shutdown")


def get_quota_manager() -> QuotaManager:
    """获取全局配额管理器单例."""
    return QuotaManager()