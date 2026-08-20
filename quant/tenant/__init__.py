"""多租户核心模块 - 租户模型、命名空间隔离、资源配额."""

from .models import Tenant, TenantStatus, ResourceQuota, ResourceUsage
from .registry import TenantRegistry, get_tenant_registry
from .namespace import NamespaceManager, get_namespace_manager
from .quota import QuotaManager, get_quota_manager
from .context import TenantContext, get_current_tenant, set_current_tenant

__all__ = [
    "Tenant",
    "TenantStatus",
    "ResourceQuota",
    "ResourceUsage",
    "TenantRegistry",
    "get_tenant_registry",
    "NamespaceManager",
    "get_namespace_manager",
    "QuotaManager",
    "get_quota_manager",
    "TenantContext",
    "get_current_tenant",
    "set_current_tenant",
]