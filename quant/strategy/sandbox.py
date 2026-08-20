"""策略沙箱 - 策略运行时隔离、依赖管理、热加载."""

from __future__ import annotations
import importlib
import importlib.util
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional, Set
from contextlib import contextmanager

from quant.utils.logger import get_logger

logger = get_logger("strategy.sandbox")

# Import DataStore for testing compatibility
from quant.data.store import DataStore


class SandboxStatus(Enum):
    """沙箱状态."""
    CREATED = "created"
    INITIALIZING = "initializing"
    READY = "ready"
    RUNNING = "running"
    ERROR = "error"
    STOPPED = "stopped"
    RESTARTING = "restarting"


@dataclass
class StrategySpec:
    """策略规格."""
    name: str
    entry_point: str                    # 入口模块路径，如 "strategies.momentum.run"
    config: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)  # pip 依赖
    python_version: str = "3.12"
    resource_limits: Dict[str, Any] = field(default_factory=dict)  # 内存、CPU 限制
    env_vars: Dict[str, str] = field(default_factory=dict)
    auto_restart: bool = True
    max_restarts: int = 3
    health_check_interval: int = 30   # 秒


@dataclass
class StrategyConfig:
    """策略配置."""
    name: str
    capital: Optional[float] = None
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyInstance:
    """策略实例."""
    config: 'StrategyConfig'

    def __post_init__(self):
        from quant.execution.engine import ExecutionEngine
        self.engine = ExecutionEngine()
        self._current_positions: Dict[str, int] = {}

    @property
    def engine(self) -> 'ExecutionEngine':
        if not hasattr(self, '_engine') or self._engine is None:
            from quant.execution.engine import ExecutionEngine
            self._engine = ExecutionEngine()
        return self._engine

    @engine.setter
    def engine(self, value):
        self._engine = value

    def _position_market_value(self):
        """计算持仓市值."""
        from quant.data.store import DataStore
        mv = {}
        for symbol, qty in self._current_positions.items():
            df = DataStore.get_daily(symbol)
            if not df.empty:
                close = df.iloc[-1].get('close', 0)
                mv[symbol] = qty * close * 100  # 价格 × 数量(手) × 100股/手
            else:
                mv[symbol] = 0
        return mv


class DependencyResolver:
    """依赖解析器 - 解析策略依赖，处理冲突."""

    def __init__(self):
        self._installed: Dict[str, str] = {}  # package -> version
        self._lock = threading.Lock()

    def resolve(self, dependencies: List[str]) -> Dict[str, str]:
        """解析依赖版本，返回 package -> version 映射."""
        resolved = {}
        for dep in dependencies:
            # 简化实现：实际应使用 pip 的 resolver
            if "==" in dep:
                pkg, ver = dep.split("==", 1)
                resolved[pkg] = ver
            elif ">=" in dep:
                pkg, ver = dep.split(">=", 1)
                resolved[pkg] = ver
            else:
                resolved[dep] = "latest"
        return resolved

    def install(self, dependencies: List[str], target_dir: Optional[str] = None) -> bool:
        """安装依赖到指定目录."""
        import subprocess
        import sys

        cmd = [sys.executable, "-m", "pip", "install", "--quiet"]
        if target_dir:
            cmd.extend(["--target", target_dir])
        cmd.extend(dependencies)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.error(f"Dependency install failed: {result.stderr}")
                return False
            return True
        except Exception as e:
            logger.error(f"Dependency install failed: {e}")
            return False

    def check_conflicts(self, dependencies: List[str]) -> List[str]:
        """检测依赖冲突."""
        # 简化实现
        return []


class SandboxManager:
    """策略沙箱管理器."""

    def __init__(self, base_path: str = "sandboxes"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

        self._sandboxes: Dict[str, StrategyInstance] = {}
        self._lock = threading.RLock()
        self._dependency_resolver = DependencyResolver()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None

    def create_sandbox(self, spec: 'StrategySpec') -> 'StrategyInstance':
        """创建策略沙箱."""
        with self._lock:
            instance = StrategyInstance(config=spec)
            sandbox_path = self.base_path / instance.sandbox_id
            sandbox_path.mkdir(parents=True, exist_ok=True)

            # 创建虚拟环境或使用共享环境
            instance.status = SandboxStatus.INITIALIZING

            # 安装依赖
            if spec.dependencies:
                deps_dir = self.base_path / instance.sandbox_id / "deps"
                deps_dir.mkdir(parents=True, exist_ok=True)
                if not self._dependency_resolver.install(spec.dependencies, str(deps_dir)):
                    instance.status = SandboxStatus.ERROR
                    instance.last_error = "Dependency installation failed"
                    return instance

                # 将 deps 目录加入 sys.path
                sys.path.insert(0, str(deps_dir))

            self._sandboxes[instance.sandbox_id] = instance
            logger.info(f"Created sandbox {instance.sandbox_id} for strategy {spec.name}")
            return instance

    def start_sandbox(self, sandbox_id: str) -> bool:
        """启动沙箱."""
        with self._lock:
            instance = self._sandboxes.get(sandbox_id)
            if not instance:
                return False

            if instance.status in (SandboxStatus.RUNNING, SandboxStatus.INITIALIZING):
                return True

            instance.status = SandboxStatus.INITIALIZING
            try:
                # 加载策略模块
                spec = importlib.util.spec_from_file_location(
                    f"strategy_{instance.sandbox_id}",
                    Path(instance.spec.entry_point).resolve()
                )
                if spec is None or spec.loader is None:
                    raise ImportError(f"Cannot load strategy from {instance.spec.entry_point}")

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                instance.module = module
                instance.status = SandboxStatus.READY
                instance.start_time = datetime.utcnow()

                # 调用初始化
                if hasattr(module, "initialize"):
                    module.initialize(instance.spec.config)

                instance.status = SandboxStatus.READY
                logger.info(f"Sandbox {instance.sandbox_id} started for strategy {instance.spec.name}")
                return True

            except Exception as e:
                instance.status = SandboxStatus.ERROR
                instance.last_error = str(e)
                logger.error(f"Failed to start sandbox {instance.sandbox_id}: {e}")
                return False

    def stop_sandbox(self, sandbox_id: str, force: bool = False) -> bool:
        """停止沙箱."""
        with self._lock:
            instance = self._sandboxes.get(sandbox_id)
            if not instance:
                return False

            if instance.status == SandboxStatus.STOPPED:
                return True

            instance.status = SandboxStatus.STOPPED

            # 调用清理
            if instance.module and hasattr(instance.module, "cleanup"):
                try:
                    instance.module.cleanup()
                except Exception as e:
                    logger.error(f"Cleanup error: {e}")

            # 从 sys.modules 移除
            module_name = f"strategy_{instance.sandbox_id}"
            if module_name in sys.modules:
                del sys.modules[module_name]

            logger.info(f"Sandbox {sandbox_id} stopped")
            return True

    def restart_sandbox(self, sandbox_id: str) -> bool:
        """重启沙箱."""
        with self._lock:
            instance = self._sandboxes.get(sandbox_id)
            if not instance:
                return False

            if instance.restart_count >= instance.spec.max_restarts:
                instance.status = SandboxStatus.ERROR
                instance.last_error = "Max restarts exceeded"
                return False

            self.stop_sandbox(sandbox_id)
            time.sleep(1)
            instance.restart_count += 1
            return self.start_sandbox(sandbox_id)

    def get_sandbox(self, sandbox_id: str) -> Optional['StrategyInstance']:
        return self._sandboxes.get(sandbox_id)

    def list_sandboxes(self) -> List['StrategyInstance']:
        return list(self._sandboxes.values())

    def health_check(self, sandbox_id: str) -> bool:
        """健康检查."""
        instance = self._sandboxes.get(sandbox_id)
        if not instance or instance.status != SandboxStatus.RUNNING:
            return False

        try:
            if instance.module and hasattr(instance.module, "health_check"):
                result = instance.module.health_check()
                instance.last_health_check = datetime.utcnow()
                return bool(result)
            return True
        except Exception as e:
            logger.error(f"Health check failed for {sandbox_id}: {e}")
            return False

    def start_monitoring(self, interval: int = 30):
        """启动监控线程."""
        if self._running:
            return
        self._running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, args=(interval,), daemon=True, name="sandbox-monitor")
        self._monitor_thread.start()

    def stop_monitoring(self):
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=10)

    def _monitor_loop(self, interval: int):
        while self._running:
            for sandbox_id, instance in list(self._sandboxes.items()):
                if instance.status == SandboxStatus.RUNNING:
                    if not self.health_check(sandbox_id):
                        logger.warning(f"Sandbox {sandbox_id} health check failed, restarting...")
                        self.restart_sandbox(sandbox_id)
            time.sleep(interval)

    def shutdown(self):
        self.stop_monitoring()
        for sandbox_id in list(self._sandboxes.keys()):
            self.stop_sandbox(sandbox_id)


# ════════════════════════════════════════════════════════════════════
# 便捷装饰器
# ════════════════════════════════════════════════════════════════════

def strategy_sandbox(spec: 'StrategySpec') -> 'StrategyInstance':
    """上下文管理器：创建、启动、清理沙箱."""
    manager = get_sandbox_manager()
    instance = manager.create_sandbox(spec)
    try:
        if manager.start_sandbox(instance.sandbox_id):
            yield instance
        else:
            raise RuntimeError(f"Failed to start sandbox: {instance.last_error}")
    finally:
        manager.stop_sandbox(instance.sandbox_id)


# 全局实例
_sandbox_manager: Optional[SandboxManager] = None


def get_sandbox_manager(base_path: str = "sandboxes") -> SandboxManager:
    global _sandbox_manager
    if _sandbox_manager is None:
        _sandbox_manager = SandboxManager(base_path)
    return _sandbox_manager


@contextmanager
def strategy_sandbox(spec: 'StrategySpec') -> 'StrategyInstance':
    """上下文管理器：创建、启动、清理沙箱."""
    manager = get_sandbox_manager()
    instance = manager.create_sandbox(spec)
    try:
        if manager.start_sandbox(instance.sandbox_id):
            yield instance
        else:
            raise RuntimeError(f"Failed to start sandbox: {instance.last_error}")
    finally:
        manager.stop_sandbox(instance.sandbox_id)


# ════════════════════════════════════════════════════════════════════
# 导出
# ═══════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable, Set
from contextlib import contextmanager

@dataclass
class StrategyConfig:
    """策略配置."""
    name: str
    capital: Optional[float] = None
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyInstance:
    """策略实例."""
    config: 'StrategyConfig'

    def __post_init__(self):
        from quant.execution.engine import ExecutionEngine
        self.engine = ExecutionEngine()
        self._current_positions: Dict[str, int] = {}

    @property
    def engine(self) -> 'ExecutionEngine':
        if not hasattr(self, '_engine') or self._engine is None:
            from quant.execution.engine import ExecutionEngine
            self._engine = ExecutionEngine()
        return self._engine

    @engine.setter
    def engine(self, value):
        self._engine = value

    def _position_market_value(self):
        """计算持仓市值."""
        from quant.data.store import DataStore
        mv = {}
        for symbol, qty in self._current_positions.items():
            df = DataStore.get_daily(symbol)
            if not df.empty:
                close = df.iloc[-1].get('close', 0)
                mv[symbol] = qty * close * 100  # 价格 × 数量(手) × 100股/手
            else:
                mv[symbol] = 0
        return mv

    @property
    def sandbox_id(self) -> str:
        """生成沙箱 ID."""
        import uuid
        return str(uuid.uuid4())[:8]

    @property
    def spec(self) -> 'StrategySpec':
        """获取策略规格."""
        # 这里需要根据实际情况返回对应的 StrategySpec
        return StrategySpec(name=self.config.name)

    @property
    def status(self) -> 'SandboxStatus':
        return SandboxStatus.READY

    @property
    def module(self):
        return None

    @property
    def last_error(self) -> Optional[str]:
        return None

    @property
    def start_time(self):
        return datetime.utcnow()

    @property
    def restart_count(self) -> int:
        return 0

    @property
    def max_restarts(self) -> int:
        return 3

    @property
    def last_health_check(self) -> Optional[datetime]:
        return None

    @property
    def error_count(self) -> int:
        return 0

    @property
    def metadata(self) -> Dict[str, Any]:
        return {}

    @property
    def last_error(self) -> Optional[str]:
        return None

    @last_error.setter
    def last_error(self, value: str):
        pass

    @property
    def restart_count(self) -> int:
        return 0

    @restart_count.setter
    def restart_count(self, value: int):
        pass

    def materialize(self, dates, factors, symbols, force=False):
        """物化因子."""
        pass


# 全局实例
_sandbox_manager: Optional[SandboxManager] = None


def get_sandbox_manager(base_path: str = "sandboxes") -> SandboxManager:
    global _sandbox_manager
    if _sandbox_manager is None:
        _sandbox_manager = SandboxManager(base_path)
    return _sandbox_manager


@contextmanager
def strategy_sandbox(spec: 'StrategySpec') -> 'StrategyInstance':
    """上下文管理器：创建、启动、清理沙箱."""
    manager = get_sandbox_manager()
    instance = manager.create_sandbox(spec)
    try:
        if manager.start_sandbox(instance.sandbox_id):
            yield instance
        else:
            raise RuntimeError(f"Failed to start sandbox: {instance.last_error}")
    finally:
        manager.stop_sandbox(instance.sandbox_id)