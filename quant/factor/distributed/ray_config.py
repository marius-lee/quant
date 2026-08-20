"""Ray 集群配置与初始化."""

from __future__ import annotations
import os
import ray
from dataclasses import dataclass
from typing import Optional
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
    custom_resources: dict = None

    def __post_init__(self):
        if self.custom_resources is None:
            self.custom_resources = {"factor_compute": 1}


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
        address = config.address or "ray://ray-head:10001"
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


# ═══════════════════════════════════════════════════════════════════
# Ray Actor 资源标注装饰器
# ═══════════════════════════════════════════════════════════════════

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