"""租户上下文管理 - 线程本地存储、上下文管理器."""

from __future__ import annotations
import threading
from contextlib import contextmanager
from typing import Optional

from .models import Tenant
from .registry import get_tenant_registry

# 线程本地存储
_thread_local = threading.local()


def get_current_tenant() -> Optional[str]:
    """获取当前线程的租户 ID."""
    return getattr(_thread_local, "tenant_id", None)


def set_current_tenant(tenant_id: str):
    """设置当前线程的租户 ID."""
    _thread_local.tenant_id = tenant_id


def clear_current_tenant():
    """清除当前线程的租户 ID."""
    if hasattr(_thread_local, "tenant_id"):
        del _thread_local.tenant_id


@contextmanager
def tenant_context(tenant_id: str):
    """租户上下文管理器."""
    from .registry import get_tenant_registry

    registry = get_tenant_registry()
    tenant = registry.get_tenant(tenant_id)

    if not tenant:
        raise ValueError(f"Tenant not found: {tenant_id}")

    if not tenant.is_active():
        raise ValueError(f"Tenant {tenant_id} is not active")

    # 保存原租户
    prev_tenant_id = get_current_tenant()

    try:
        set_current_tenant(tenant_id)
        yield tenant
    finally:
        if prev_tenant_id:
            set_current_tenant(prev_tenant_id)
        else:
            clear_current_tenant()


def get_current_tenant_obj() -> Optional["Tenant"]:
    """获取当前租户对象."""
    tenant_id = get_current_tenant()
    if tenant_id:
        from .registry import get_tenant_registry
        return get_tenant_registry().get_tenant(tenant_id)
    return None


def require_tenant(tenant_id: Optional[str] = None) -> str:
    """获取当前租户 ID，如果未设置则抛出异常或使用指定值."""
    current = get_current_tenant()
    if current:
        return current
    if tenant_id:
        return tenant_id
    raise RuntimeError("No tenant context set. Use tenant_context() or set_current_tenant().")


def get_tenant_quota(resource_type: str) -> Optional[float]:
    """获取当前租户的资源配额."""
    tenant = get_current_tenant_obj()
    if not tenant:
        return None
    from quant.tenant.models import ResourceType
    try:
        rt = ResourceType(resource_type)
        quota = tenant.get_quota(rt)
        return quota.hard_limit if quota else None
    except ValueError:
        return None


def check_tenant_quota(resource_type: str, requested: float = 1.0) -> tuple[bool, str]:
    """检查当前租户配额."""
    tenant = get_current_tenant_obj()
    if not tenant:
        return False, "no tenant context"
    from quant.tenant.models import ResourceType
    try:
        rt = ResourceType(resource_type)
        return tenant.check_quota(rt, requested)
    except ValueError:
        return False, f"invalid resource type: {resource_type}"


def record_tenant_usage(resource_type: str, usage: float):
    """记录当前租户资源使用量."""
    tenant = get_current_tenant_obj()
    if tenant:
        from quant.tenant.models import ResourceType
        try:
            rt = ResourceType(resource_type)
            tenant.record_usage(rt, usage)
        except ValueError:
            pass


def require_tenant_admin(tenant_id: Optional[str] = None) -> str:
    """要求租户管理员权限."""
    current = get_current_tenant() or tenant_id
    if not current:
        raise PermissionError("No tenant context")

    from .registry import get_tenant_registry
    registry = get_tenant_registry()
    tenant = registry.get_tenant(current)

    if not tenant or not tenant.is_admin(require_tenant_admin.__globals__["_thread_local"].tenant_id):
        raise PermissionError("Insufficient permissions: admin required")

    return current


def require_tenant_member(tenant_id: Optional[str] = None) -> str:
    """要求租户成员权限."""
    current = get_current_tenant() or tenant_id
    if not current:
        raise PermissionError("No tenant context")

    from .registry import get_tenant_registry
    registry = get_tenant_registry()
    tenant = registry.get_tenant(current)

    if not tenant or not tenant.can_access(require_tenant_member.__globals__["_thread_local"].tenant_id):
        raise PermissionError("Insufficient permissions: member required")

    return current