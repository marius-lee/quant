"""Ray 集群配置与初始化 — 增强版：支持 KubeRay CRD、Actor 池、内存保护."""

from __future__ import annotations
import os
import ray
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

logger = get_logger("factor.distributed.ray_config")


@dataclass
class RayConfig:
    """Ray 运行时配置."""
    # 集群模式
    mode: str = "local"  # "local" | "cluster" | "k8s"
    # Head 节点地址 (cluster 模式)
    address: Optional[str] = None
    # 资源配置
    num_cpus: Optional[int] = None  # None = 自动检测
    num_gpus: int = 0
    object_store_memory: Optional[int] = None  # bytes, None = 默认
    # 内存管理
    memory_monitor_refresh_ms: int = 1000
    object_spilling_config: Optional[dict] = None
    # 日志
    log_to_driver: bool = True
    driver_object_store_memory: int = 100 * 1024 * 1024  # 100MB
    # 调度
    max_calls_per_worker: int = 100  # Worker 回收阈值, 防内存泄漏
    # 自定义资源 (用于因子计算隔离)
    custom_resources: dict = field(default_factory=lambda: {"factor_compute": 1})
    # KubeRay 特定配置
    kuberay_namespace: str = "ray-system"
    kubeconfig: Optional[str] = None
    # Actor 池配置
    actor_pool_size: int = 0  # 0 = 不使用 Actor 池
    actor_idle_timeout: int = 300  # Actor 空闲超时(秒)


def get_ray_config() -> RayConfig:
    """从 config.yaml 读取 Ray 配置."""
    from quant.config.loader import load as _load_config
    cfg = _load_config()
    ray_cfg = cfg.get("factor", {}).get("distributed", {}).get("ray", {})

    return RayConfig(
        mode=ray_cfg.get("mode", "local"),
        address=ray_cfg.get("address"),
        num_cpus=ray_cfg.get("num_cpus"),
        num_gpus=ray_cfg.get("num_gpus", 0),
        object_store_memory=ray_cfg.get("object_store_memory"),
        max_calls_per_worker=ray_cfg.get("max_calls_per_worker", 100),
        custom_resources=ray_cfg.get("custom_resources", {"factor_compute": 1}),
        kuberay_namespace=ray_cfg.get("kuberay_namespace", "ray-system"),
        kubeconfig=ray_cfg.get("kubeconfig"),
        actor_pool_size=ray_cfg.get("actor_pool_size", 0),
        actor_idle_timeout=ray_cfg.get("actor_idle_timeout", 300),
    )


def init_ray(config: Optional[RayConfig] = None) -> ray.Client:
    """初始化 Ray 运行时.

    支持三种模式:
      - local: 本地单机多进程 (开发/测试/小规模)
      - cluster: 连接现有 Ray 集群 (生产)
      - k8s: KubeRay Operator 管理 (生产 K8s)
    """
    config = config or get_ray_config()

    if ray.is_initialized():
        logger.info("Ray already initialized")
        return ray

    init_kwargs = {
        "log_to_driver": config.log_to_driver,
        "driver_object_store_memory": config.driver_object_store_memory,
        "max_calls_per_worker": config.max_calls_per_worker,
    }

    if config.num_cpus:
        init_kwargs["num_cpus"] = config.num_cpus
    if config.num_gpus:
        init_kwargs["num_gpus"] = config.num_gpus
    if config.object_store_memory:
        init_kwargs["object_store_memory"] = config.object_store_memory
    if config.object_spilling_config:
        init_kwargs["_system_config"] = {
            "object_spilling_config": config.object_spilling_config
        }

    if config.mode == "local":
        logger.info(f"Starting Ray local mode: cpus={config.num_cpus or 'auto'}")
        ray.init(**init_kwargs)

    elif config.mode == "cluster":
        if not config.address:
            raise ValueError("cluster mode requires address")
        logger.info(f"Connecting to Ray cluster: {config.address}")
        ray.init(address=config.address, **init_kwargs)

    elif config.mode == "k8s":
        # KubeRay: 通过 head service 连接
        address = config.address or f"ray://ray-head.{config.kuberay_namespace}.svc:10001"
        logger.info(f"Connecting to KubeRay: {address}")
        ray.init(address=address, **init_kwargs)

    else:
        raise ValueError(f"Unknown Ray mode: {config.mode}")

    # 打印集群资源摘要
    resources = ray.available_resources()
    logger.info(f"Ray initialized. Available resources: {resources}")
    logger.info(f"Ray Dashboard: {ray.dashboard_url}")

    return ray


def shutdown_ray():
    """关闭 Ray."""
    if ray.is_initialized():
        ray.shutdown()
        logger.info("Ray shutdown")


# ════════════════════════════════════════════════════════════════════
# Ray Actor 资源标注装饰器
# ════════════════════════════════════════════════════════════════════

def factor_actor(num_cpus: float = 1.0, memory: int = None):
    """因子计算 Actor 装饰器 - 预留 factor_compute 资源."""
    def decorator(cls):
        resources = {"factor_compute": 0.01}  # 软标记, 用于调度亲和性
        if num_cpus:
            resources["num_cpus"] = num_cpus
        if memory:
            resources["memory"] = memory
        return ray.remote(resources=resources)(cls)
    return decorator


def factor_task(num_cpus: float = 1.0, memory: int = None, max_retries: int = 3):
    """因子计算 Task 装饰器 - 自动重试 + 资源声明."""
    def decorator(fn):
        return ray.remote(
            num_cpus=num_cpus,
            memory=memory,
            max_retries=max_retries,
            retry_exceptions=True,
        )(fn)
    return decorator


# ════════════════════════════════════════════════════════════════════
# Actor 池管理
# ═══════════════════════════════════════════════════════════════════

class FactorStoreActorPool:
    """FactorStore Actor 池 — 复用 DB 连接，避免每 Task 重复初始化."""

    def __init__(
        self,
        pool_size: int,
        factor_store_config: dict,
        idle_timeout: int = 300,
    ):
        self.pool_size = pool_size
        self.factor_store_config = factor_store_config
        self.idle_timeout = idle_timeout
        self._actors: list = []
        self._available: list = []
        self._in_use: set = set()
        self._last_used: dict = {}
        self._initialized = False

    def initialize(self):
        """初始化 Actor 池."""
        if self._initialized:
            return

        @ray.remote(num_cpus=0, resources={"factor_compute": 0.01})
        class FactorStoreActor:
            def __init__(self, config):
                from quant.factor.store import FactorStore
                self.store = FactorStore(**config)
                self.last_used = time.time()

            def materialize(self, dates, factors, symbols, force=False):
                self.last_used = time.time()
                result = self.store.materialize(dates, factors, symbols, force)
                return result

            def health_check(self):
                return {"status": "healthy", "last_used": self.last_used}

            def close(self):
                self.store.close()
                return True

        self._ActorClass = ray.remote(num_cpus=0, resources={"factor_compute": 0.01})(FactorStoreActor)

        # 创建 Actor 实例
        for _ in range(self.pool_size):
            actor = self._ActorClass.remote(self.factor_store_config)
            self._actors.append(actor)
            self._available.append(actor)

        self._initialized = True
        logger.info(f"FactorStoreActorPool initialized with {self.pool_size} actors")

    def acquire(self, timeout: float = 30.0):
        """获取一个可用 Actor."""
        start = time.time()
        while time.time() - start < timeout:
            if self._available:
                actor = self._available.pop()
                self._in_use.add(actor)
                self._last_used[actor] = time.time()
                return actor
            time.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for available actor after {timeout}s")

    def release(self, actor):
        """释放 Actor 回池."""
        if actor in self._in_use:
            self._in_use.remove(actor)
            self._last_used[actor] = time.time()
            self._available.append(actor)

    def health_check(self):
        """检查所有 Actor 健康状态."""
        now = time.time()
        results = []
        for actor in self._actors:
            try:
                result = ray.get(actor.health_check.remote(), timeout=5)
                results.append(result)
            except Exception as e:
                results.append({"status": "unhealthy", "error": str(e)})

        # 清理超时空闲 Actor
        idle_actors = [a for a in self._available if now - self._last_used.get(a, now) > self.idle_timeout]
        for actor in idle_actors:
            if len(self._actors) > 1:  # 保留至少 1 个
                try:
                    ray.kill(actor)
                    self._actors.remove(actor)
                    self._available.remove(actor)
                    logger.info(f"Removed idle actor after {self.idle_timeout}s timeout")
                except Exception:
                    pass

        return results

    def shutdown(self):
        """关闭所有 Actor."""
        for actor in self._actors:
            try:
                ray.kill(actor)
            except Exception:
                pass
        self._actors.clear()
        self._available.clear()
        self._in_use.clear()
        logger.info("FactorStoreActorPool shutdown")


# 全局 Actor 池实例
_actor_pool: Optional[FactorStoreActorPool] = None


def get_actor_pool(
    pool_size: int = 0,
    factor_store_config: Optional[dict] = None,
    idle_timeout: int = 300,
) -> Optional[FactorStoreActorPool]:
    """获取或创建全局 Actor 池."""
    global _actor_pool
    if _actor_pool is None and pool_size > 0:
        _actor_pool = FactorStoreActorPool(
            pool_size=pool_size,
            factor_store_config=factor_store_config or {"db_path": "quant/data/factor_cache.db"},
            idle_timeout=idle_timeout,
        )
        _actor_pool.initialize()
    return _actor_pool


def shutdown_actor_pool():
    """关闭全局 Actor 池."""
    global _actor_pool
    if _actor_pool is not None:
        _actor_pool.shutdown()
        _actor_pool = None


# ════════════════════════════════════════════════════════════════════
# 内存压力保护
# ═══════════════════════════════════════════════════════════════════

class MemoryPressureMonitor:
    """内存压力监控 - 监控 Ray 对象存储和系统内存，触发保护机制."""

    def __init__(
        self,
        object_store_threshold: float = 0.85,  # 对象存储使用率阈值
        system_memory_threshold: float = 0.85,  # 系统内存使用率阈值
        check_interval: int = 10,  # 检查间隔(秒)
    ):
        self.object_store_threshold = object_store_threshold
        self.system_memory_threshold = system_memory_threshold
        self.check_interval = check_interval
        self._running = False
        self._thread = None

    def start(self):
        """启动监控线程."""
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("MemoryPressureMonitor started")

    def stop(self):
        """停止监控线程."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _monitor_loop(self):
        import psutil
        while self._running:
            try:
                # 检查 Ray 对象存储
                try:
                    stats = ray.available_resources()
                    # 注意: ray.available_resources() 可能不直接给出 object_store 用量
                    # 可通过 ray._private.services.get_object_store_memory() 获取
                except Exception:
                    pass

                # 检查系统内存
                import psutil
                mem = psutil.virtual_memory()
                if mem.percent / 100 > self.system_memory_threshold:
                    logger.warning(f"System memory pressure high: {mem.percent}%")
                    # 触发 GC
                    import gc
                    gc.collect()

                time.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"Memory monitor error: {e}")
                time.sleep(self.check_interval)


# 全局内存监控实例
_memory_monitor: Optional[MemoryPressureMonitor] = None


def get_memory_monitor() -> Optional[MemoryPressureMonitor]:
    global _memory_monitor
    return _memory_monitor


def start_memory_monitor(
    object_store_threshold: float = 0.85,
    system_memory_threshold: float = 0.85,
    check_interval: int = 10,
):
    """启动内存压力监控."""
    global _memory_monitor
    if _memory_monitor is None:
        _memory_monitor = MemoryPressureMonitor(
            object_store_threshold=object_store_threshold,
            system_memory_threshold=system_memory_threshold,
            check_interval=check_interval,
        )
    _memory_monitor.start()


def stop_memory_monitor():
    """停止内存压力监控."""
    global _memory_monitor
    if _memory_monitor:
        _memory_monitor.stop()
        _memory_monitor = None


# ═══════════════════════════════════════════════════════════════════
# 分区策略自动选择
# ═══════════════════════════════════════════════════════════════════

def auto_select_partition_strategy(
    num_factors: int,
    num_symbols: int,
    num_dates: int,
    cluster_cpus: int,
) -> tuple[str, dict]:
    """根据工作负载自动选择最优分区策略.

    Args:
        num_factors: 因子数量
        num_symbols: 股票数量
        num_dates: 交易日数
        cluster_cpus: 集群 CPU 核心数

    Returns:
        (strategy_name, partition_kwargs)
    """
    total_work = num_factors * num_symbols * num_dates

    # 小规模: 单机足够，按日期分区即可
    if total_work < 50000 and cluster_cpus <= 8:
        return "date", {"max_partition_size": 50000}

    # 中等规模: 日期 × 因子 组合
    if total_work < 500000:
        dates_per_partition = max(1, min(10, 50000 // (len(factors) * num_symbols) if num_factors * num_symbols > 0 else 5))
        return "composite", {
            "max_partition_size": 50000,
            "dates_per_partition": dates_per_partition,
            "factors_per_partition": min(20, max(1, 50000 // (dates_per_partition * num_symbols))),
        }

    # 大规模: 因子并行为主
    if num_factors > 50:
        return "factor", {"factors_per_partition": max(10, num_factors // cluster_cpus)}

    # 默认组合策略
    return "composite", {
        "max_partition_size": 50000,
        "dates_per_partition": 5,
        "factors_per_partition": 20,
    }