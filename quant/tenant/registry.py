"""租户注册表 - 单例管理所有租户实例."""

from __future__ import annotations
import threading
import time
from typing import Dict, List, Optional, Callable
from pathlib import Path

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

from .models import Tenant, TenantStatus, ResourceType, ResourceQuota

logger = get_logger("tenant.registry")


class TenantRegistry:
    """租户注册表 - 单例."""

    _instance: Optional["TenantRegistry"] = None
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

        self._tenants: Dict[str, Tenant] = {}
        self._name_to_id: Dict[str, str] = {}
        self._lock = threading.RLock()

        # 配置
        self._config_path = Path(_require_cfg("tenant.config_path", "config/tenants.yaml"))
        self._auto_provision = _require_cfg("tenant.auto_provision", True)

        # 回调钩子
        self._on_tenant_created: List[Callable[[str], None]] = []
        self._on_tenant_updated: List[Callable[[str], None]] = []
        self._on_tenant_deleted: List[Callable[[str], None]] = []

        # 后台任务
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None

    def register_tenant(self, tenant: Tenant) -> bool:
        """注册新租户."""
        with self._lock:
            if tenant.id in self._tenants:
                logger.warning(f"Tenant {tenant.id} already exists")
                return False

            if tenant.name in self._name_to_id:
                logger.warning(f"Tenant name {tenant.name} already exists")
                return False

            self._tenants[tenant.id] = tenant
            self._name_to_id[tenant.name] = tenant.id

            # 触发回调
            for cb in self._on_tenant_created:
                try:
                    cb(tenant.id)
                except Exception as e:
                    logger.error(f"Tenant created callback error: {e}")

            logger.info(f"Registered tenant: {tenant.id} ({tenant.name})")
            return True

    def unregister_tenant(self, tenant_id: str, force: bool = False) -> bool:
        """注销租户."""
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if not tenant:
                logger.warning(f"Tenant {tenant_id} not found")
                return False

            if tenant.status == TenantStatus.ACTIVE and not force:
                logger.warning(f"Tenant {tenant_id} is active, use force=True to delete")
                return False

            # 标记为删除中
            tenant.status = TenantStatus.DEPROVISIONING

            # 从索引移除
            del self._tenants[tenant_id]
            del self._name_to_id[tenant.name]

            # 触发回调
            for cb in self._on_tenant_deleted:
                try:
                    cb(tenant_id)
                except Exception as e:
                    logger.error(f"Tenant deleted callback error: {e}")

            logger.info(f"Unregistered tenant: {tenant_id}")
            return True

    def get_tenant(self, tenant_id: str) -> Optional[Tenant]:
        """获取租户."""
        return self._tenants.get(tenant_id)

    def get_tenant_by_name(self, name: str) -> Optional[Tenant]:
        """按名称获取租户."""
        tenant_id = self._name_to_id.get(name)
        if tenant_id:
            return self._tenants.get(tenant_id)
        return None

    def list_tenants(self, status: Optional[TenantStatus] = None) -> List[Tenant]:
        """列出租户."""
        with self._lock:
            tenants = list(self._tenants.values())
            if status:
                tenants = [t for t in tenants if t.status == status]
            return tenants

    def update_tenant(self, tenant_id: str, **kwargs) -> bool:
        """更新租户信息."""
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if not tenant:
                return False

            # 更新字段
            for key, value in kwargs.items():
                if hasattr(tenant, key) and key not in ("id", "created_at"):
                    # 特殊处理：名称变更需更新索引
                    if key == "name" and value != tenant.name:
                        if value in self._name_to_id:
                            logger.warning(f"Tenant name {value} already exists")
                            return False
                        del self._name_to_id[tenant.name]
                        self._name_to_id[value] = tenant.id

                    setattr(tenant, key, value)

            tenant.updated_at = datetime.utcnow()

            # 触发回调
            for cb in self._on_tenant_updated:
                try:
                    cb(tenant_id)
                except Exception as e:
                    logger.error(f"Tenant updated callback error: {e}")

            logger.info(f"Updated tenant: {tenant_id}")
            return True

    def suspend_tenant(self, tenant_id: str, reason: str = "") -> bool:
        """暂停租户."""
        return self.update_tenant(tenant_id, status=TenantStatus.SUSPENDED)

    def resume_tenant(self, tenant_id: str) -> bool:
        """恢复租户."""
        return self.update_tenant(tenant_id, status=TenantStatus.ACTIVE)

    def archive_tenant(self, tenant_id: str) -> bool:
        """归档租户."""
        return self.update_tenant(tenant_id, status=TenantStatus.ARCHIVED)

    # ══════════════════════════════════════════════════════════════════
    # 回调钩子
    # ══════════════════════════════════════════════════════════════════

    def on_tenant_created(self, callback: Callable[[str], None]):
        self._on_tenant_created.append(callback)

    def on_tenant_updated(self, callback: Callable[[str], None]):
        self._on_tenant_updated.append(callback)

    def on_tenant_deleted(self, callback: Callable[[str], None]):
        self._on_tenant_deleted.append(callback)

    # ══════════════════════════════════════════════════════════════════
    # 配额监控
    # ══════════════════════════════════════════════════════════════════

    def check_all_quotas(self) -> Dict[str, List[str]]:
        """检查所有租户配额，返回 {tenant_id: [warning_messages]}."""
        warnings = {}
        with self._lock:
            for tenant_id, tenant in self._tenants.items():
                if not tenant.is_active():
                    continue

                warnings_list = []
                for resource_type in ResourceType:
                    quota = tenant.get_quota(resource_type)
                    usage = tenant.get_usage(resource_type)
                    if quota and usage:
                        if usage.current_usage > quota.hard_limit:
                            warnings_list.append(
                                f"QUOTA_EXCEEDED: {resource_type.value} "
                                f"({usage.current_usage}/{quota.hard_limit} {quota.unit})"
                            )
                        elif usage.current_usage > quota.soft_limit:
                            warnings_list.append(
                                f"QUOTA_WARNING: {resource_type.value} "
                                f"({usage.current_usage}/{quota.soft_limit} {quota.unit})"
                            )
                if warnings_list:
                    warnings[tenant_id] = warnings_list
        return warnings

    # ══════════════════════════════════════════════════════════════════
    # 配置持久化
    # ══════════════════════════════════════════════════════════════════

    def save_to_config(self) -> bool:
        """保存租户配置到 YAML."""
        try:
            import yaml
            data = {
                "tenants": [
                    {
                        "id": t.id,
                        "name": t.name,
                        "display_name": t.display_name,
                        "description": t.description,
                        "status": t.status.value,
                        "owner_id": t.owner_id,
                        "admin_ids": t.admin_ids,
                        "member_ids": t.member_ids,
                        "namespace_prefix": t.namespace_prefix,
                        "labels": t.labels,
                        "annotations": t.annotations,
                        "quotas": {
                            rt.value: {"hard": q.hard_limit, "soft": q.soft_limit, "unit": q.unit}
                            for rt, q in t.quotas.items()
                        },
                    }
                    for t in self._tenants.values()
                ]
            }
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._config_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, sort_keys=False)
            logger.info(f"Saved {len(self._tenants)} tenants to config")
            return True
        except Exception as e:
            logger.error(f"Failed to save tenant config: {e}")
            return False

    def load_from_config(self) -> bool:
        """从 YAML 加载租户配置."""
        try:
            import yaml
            if not self._config_path.exists():
                logger.info("No tenant config file found, skipping load")
                return True

            with open(self._config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            for tenant_data in data.get("tenants", []):
                tenant_id = tenant_data.get("id") or str(uuid4())
                tenant = Tenant(
                    id=tenant_id,
                    name=tenant_data["name"],
                    display_name=tenant_data.get("display_name", tenant_data["name"]),
                    description=tenant_data.get("description", ""),
                    status=TenantStatus(tenant_data.get("status", "active")),
                    owner_id=tenant_data.get("owner_id", ""),
                    admin_ids=tenant_data.get("admin_ids", []),
                    member_ids=tenant_data.get("member_ids", []),
                    namespace_prefix=tenant_data.get("namespace_prefix", f"tenant_{tenant_id[:8]}"),
                    labels=tenant_data.get("labels", {}),
                    annotations=tenant_data.get("annotations", {}),
                )

                # 加载配额
                for rt_str, qcfg in tenant_data.get("quotas", {}).items():
                    try:
                        rt = ResourceType(rt_str)
                        tenant.quotas[rt] = ResourceQuota(
                            resource_type=rt,
                            hard_limit=qcfg["hard"],
                            soft_limit=qcfg["soft"],
                            unit=qcfg["unit"],
                        )
                    except ValueError:
                        logger.warning(f"Unknown resource type in config: {rt_str}")

                self._tenants[tenant_id] = tenant
                self._name_to_id[tenant.name] = tenant_id
                logger.info(f"Loaded tenant: {tenant_id} ({tenant.name})")

            return True
        except Exception as e:
            logger.error(f"Failed to load tenant config: {e}")
            return False

    # ══════════════════════════════════════════════════════════════════
    # 后台监控
    # ══════════════════════════════════════════════════════════════════

    def start_monitoring(self, interval: int = 60):
        """启动配额监控后台任务."""
        if self._running:
            return
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(interval,),
            daemon=True,
            name="tenant-quota-monitor"
        )
        self._monitor_thread.start()
        logger.info("Tenant quota monitoring started")

    def stop_monitoring(self):
        """停止监控."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=10)

    def _monitor_loop(self, interval: int):
        while self._running:
            try:
                warnings = self.check_all_quotas()
                for tenant_id, warnings_list in warnings.items():
                    tenant = self._tenants.get(tenant_id)
                    if tenant:
                        for warn in warnings_list:
                            if "QUOTA_EXCEEDED" in warn:
                                logger.error(f"TENANT QUOTA EXCEEDED: {tenant.name} ({tenant_id}) - {warn}")
                            else:
                                logger.warning(f"TENANT QUOTA WARNING: {tenant.name} ({tenant_id}) - {warn}")
            except Exception as e:
                logger.error(f"Quota monitoring error: {e}")
            time.sleep(interval)

    def shutdown(self):
        """关闭注册表."""
        self.stop_monitoring()
        self.save_to_config()
        logger.info("Tenant registry shutdown")


def get_tenant_registry() -> TenantRegistry:
    """获取全局租户注册表单例."""
    return TenantRegistry()