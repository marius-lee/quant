"""命名空间管理器 - 数据隔离、表前缀、权限控制."""

from __future__ import annotations
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

from .models import Tenant, ResourceType

logger = get_logger("tenant.namespace")


@dataclass
class NamespaceConfig:
    """命名空间配置."""
    tenant_id: str
    prefix: str                    # 表前缀，如 tenant_abc123_
    schema: str = "public"         # 数据库 schema
    tables: List[str] = field(default_factory=list)  # 该租户拥有的表
    read_only_tables: Set[str] = field(default_factory=set)  # 只读表
    writable_tables: Set[str] = field(default_factory=set)   # 可写表


class NamespaceManager:
    """命名空间管理器 - 管理租户数据隔离."""

    _instance: Optional["NamespaceManager"] = None
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

        self._namespaces: Dict[str, NamespaceConfig] = {}
        self._lock = threading.RLock()
        self._table_ownership: Dict[str, str] = {}  # table -> tenant_id

    def register_tenant_namespace(
        self,
        tenant_id: str,
        prefix: Optional[str] = None,
        schema: str = "public",
        tables: Optional[List[str]] = None,
    ) -> str:
        """注册租户命名空间."""
        from quant.tenant.registry import get_tenant_registry
        registry = get_tenant_registry()
        tenant = registry.get_tenant(tenant_id)

        if not tenant:
            raise ValueError(f"Tenant not found: {tenant_id}")

        prefix = prefix or tenant.namespace_prefix
        config = NamespaceConfig(
            tenant_id=tenant_id,
            prefix=prefix,
            schema=schema,
            tables=tables or [],
        )

        with self._lock:
            self._namespaces[tenant_id] = config
            # 记录表所有权
            for table in config.tables:
                self._table_ownership[table] = tenant_id

        logger.info(f"Registered namespace for tenant {tenant_id}: prefix={prefix}, schema={schema}")
        return prefix

    def unregister_tenant_namespace(self, tenant_id: str) -> bool:
        """注销租户命名空间."""
        with self._lock:
            config = self._namespaces.pop(tenant_id, None)
            if not config:
                return False

            # 清理表所有权
            for table in config.tables:
                self._table_ownership.pop(table, None)

        logger.info(f"Unregistered namespace for tenant {tenant_id}")
        return True

    def get_namespace(self, tenant_id: str) -> Optional[NamespaceConfig]:
        """获取命名空间配置."""
        return self._namespaces.get(tenant_id)

    def get_table_prefix(self, tenant_id: str) -> str:
        """获取表前缀."""
        config = self._namespaces.get(tenant_id)
        return config.prefix if config else ""

    def get_full_table_name(self, tenant_id: str, logical_table: str) -> str:
        """获取带前缀的完整表名."""
        prefix = self.get_table_prefix(tenant_id)
        return f"{prefix}{logical_table}"

    def get_tenant_by_table(self, table: str) -> Optional[str]:
        """通过表名反查租户."""
        return self._table_ownership.get(table)

    def is_table_readonly(self, tenant_id: str, table: str) -> bool:
        """判断表是否只读."""
        config = self._namespaces.get(tenant_id)
        if not config:
            return True
        return table in config.read_only_tables

    def is_table_writable(self, tenant_id: str, table: str) -> bool:
        """判断表是否可写."""
        config = self._namespaces.get(tenant_id)
        if not config:
            return False
        if config.writable_tables:
            return table in config.writable_tables
        return table not in config.read_only_tables

    def set_table_permissions(
        self,
        tenant_id: str,
        read_only_tables: Optional[List[str]] = None,
        writable_tables: Optional[List[str]] = None,
    ) -> bool:
        """设置表权限."""
        config = self._namespaces.get(tenant_id)
        if not config:
            return False

        if read_only_tables is not None:
            config.read_only_tables = set(read_only_tables)
        if writable_tables is not None:
            config.writable_tables = set(writable_tables)
        return True

    def list_tenant_tables(self, tenant_id: str) -> List[str]:
        """获取租户的所有表."""
        config = self._namespaces.get(tenant_id)
        return config.tables if config else []

    def validate_table_access(self, tenant_id: str, table: str, write: bool = False) -> bool:
        """验证表访问权限."""
        config = self._namespaces.get(tenant_id)
        if not config:
            return False

        if write:
            return self.is_table_writable(tenant_id, table)
        return not self.is_table_readonly(tenant_id, table)

    def get_all_namespaces(self) -> Dict[str, NamespaceConfig]:
        """获取所有命名空间."""
        return dict(self._namespaces)


def get_namespace_manager() -> NamespaceManager:
    """获取全局命名空间管理器单例."""
    return NamespaceManager()