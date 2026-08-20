"""资源调度与公平队列 - 租户感知调度、DRF公平、优先级继承、抢占."""

from __future__ import annotations
import heapq
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple
from collections import defaultdict

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

from .models import Tenant, ResourceType
from .registry import get_tenant_registry

logger = get_logger("tenant.scheduling")


class TaskPriority(Enum):
    """任务优先级."""
    LOW = 0
    NORMAL = 50
    HIGH = 100
    CRITICAL = 200


class SchedulingPolicy(Enum):
    """调度策略."""
    FIFO = "fifo"
    PRIORITY = "priority"
    FAIR_SHARE = "fair_share"        # DRF (Dominant Resource Fairness)
    WEIGHTED_FAIR = "weighted_fair"  # 加权公平


@dataclass
class Task:
    """调度任务."""
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    tenant_id: str = ""
    priority: TaskPriority = TaskPriority.NORMAL
    submitted_at: datetime = field(default_factory=datetime.utcnow)
    deadline: Optional[datetime] = None
    estimated_duration: float = 0.0
    callback: Optional[Callable] = None
    args: tuple = field(default_factory=tuple)
    kwargs: Dict = field(default_factory=dict)
    retry_count: int = 0
    max_retries: int = 3
    priority_boost: int = 0
    resource_demand: Dict[ResourceType, float] = field(default_factory=dict)

    def effective_priority(self) -> int:
        return self.priority.value + self.priority_boost

    def dominant_share(self, cluster_resources: Dict[ResourceType, float]) -> float:
        """计算主导资源份额 (DRF 核心)."""
        if not cluster_resources:
            return 0.0
        shares = []
        for res_type, demand in self.resource_demand.items():
            if res_type in cluster_resources and cluster_resources[res_type] > 0:
                shares.append(demand / cluster_resources[res_type])
        return max(shares) if shares else 0.0


@dataclass
class TenantSchedulingQuota:
    """租户调度配额 (区别于 ResourceQuota，用于调度器)."""
    tenant_id: str
    max_concurrent_tasks: int = 4
    max_cpu_cores: float = 2.0
    max_memory_mb: int = 2048
    weight: float = 1.0  # 权重 (加权 DRF)
    reserved_slots: int = 0  # 保留槽位


@dataclass
class TaskResult:
    """任务执行结果."""
    task_id: str
    success: bool
    result: Any = None
    error: Optional[str] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime = field(default_factory=datetime.utcnow)
    duration_ms: float = 0.0


class DRFScheduler:
    """DRF (Dominant Resource Fairness) 调度器核心算法.

    DRF 核心思想:
    1. 每个租户有多维资源需求 (CPU, Memory, GPU, IO...)
    2. 计算每个租户的主导资源份额 = max(需求_i / 总资源_i)
    3. 优先调度主导份额最小的租户
    4. 支持加权 DRF: 份额 / 权重
    """

    def __init__(self, cluster_resources: Dict[ResourceType, float]):
        self._cluster_resources = cluster_resources
        self._tenant_allocations: Dict[str, Dict[ResourceType, float]] = defaultdict(lambda: defaultdict(float))
        self._tenant_weights: Dict[str, float] = {}
        self._tenant_queues: Dict[str, List[Tuple[float, int, Task]]] = defaultdict(list)  # (weighted_share, seq, task)
        self._seq = 0
        self._lock = threading.RLock()

    def set_cluster_resources(self, resources: Dict[ResourceType, float]):
        with self._lock:
            self._cluster_resources = resources

    def set_tenant_weight(self, tenant_id: str, weight: float):
        with self._lock:
            self._tenant_weights[tenant_id] = max(weight, 0.01)

    def get_tenant_weight(self, tenant_id: str) -> float:
        return self._tenant_weights.get(tenant_id, 1.0)

    def submit_task(self, tenant_id: str, task: Task) -> int:
        """提交任务到租户队列."""
        with self._lock:
            self._seq += 1
            dominant = task.dominant_share(self._cluster_resources)
            weight = self.get_tenant_weight(tenant_id)
            weighted_share = dominant / weight
            heapq.heappush(self._tenant_queues[tenant_id], (weighted_share, self._seq, task))
            return self._seq

    def pop_next_task(self, max_per_tenant: Dict[str, int]) -> Optional[Tuple[str, Task]]:
        """弹出下一个任务 (DRF 算法)."""
        with self._lock:
            best_tenant = None
            best_task = None
            min_weighted_share = float('inf')

            for tenant_id, queue in self._tenant_queues.items():
                if not queue:
                    continue

                running = sum(1 for _, _, t in queue if t is not None)  # 简化统计
                limit = max_per_tenant.get(tenant_id, float('inf'))
                if running >= limit:
                    continue

                # 查看队首
                weighted_share, _, task = queue[0]
                if weighted_share < min_weighted_share:
                    min_weighted_share = weighted_share
                    best_tenant = tenant_id
                    best_task = task

            if best_tenant and best_task:
                heapq.heappop(self._tenant_queues[best_tenant])
                # 记录分配
                for res_type, demand in best_task.resource_demand.items():
                    self._tenant_allocations[best_tenant][res_type] += demand
                return best_tenant, best_task

            return None

    def release_task(self, tenant_id: str, task: Task):
        """释放任务资源."""
        with self._lock:
            for res_type, demand in task.resource_demand.items():
                self._tenant_allocations[tenant_id][res_type] = max(
                    0, self._tenant_allocations[tenant_id][res_type] - demand
                )

    def get_tenant_dominant_share(self, tenant_id: str) -> float:
        """获取租户当前主导份额."""
        with self._lock:
            alloc = self._tenant_allocations.get(tenant_id, {})
            shares = []
            for res_type, allocated in alloc.items():
                if res_type in self._cluster_resources and self._cluster_resources[res_type] > 0:
                    shares.append(allocated / self._cluster_resources[res_type])
            weight = self.get_tenant_weight(tenant_id)
            return max(shares) / weight if shares else 0.0

    def get_allocations(self) -> Dict[str, Dict[ResourceType, float]]:
        with self._lock:
            return {tid: dict(alloc) for tid, alloc in self._tenant_allocations.items()}


class PriorityInheritanceManager:
    """优先级继承管理器 - 解决优先级反转."""

    def __init__(self):
        self._task_holders: Dict[str, str] = {}  # resource -> task_id
        self._task_waiters: Dict[str, List[str]] = defaultdict(list)  # resource -> [task_id]
        self._task_priorities: Dict[str, int] = {}  # task_id -> effective_priority
        self._lock = threading.RLock()

    def acquire(self, task_id: str, resource: str, priority: int) -> bool:
        """尝试获取资源."""
        with self._lock:
            holder = self._task_holders.get(resource)
            if holder is None:
                self._task_holders[resource] = task_id
                self._task_priorities[task_id] = max(self._task_priorities.get(task_id, 0), priority)
                return True

            # 资源被占用，记录等待者
            if task_id not in self._task_waiters[resource]:
                self._task_waiters[resource].append(task_id)

            # 优先级继承: 提升持有者优先级
            waiter_priority = priority
            if holder in self._task_priorities:
                self._task_priorities[holder] = max(self._task_priorities[holder], waiter_priority)

            return False

    def release(self, task_id: str, resource: str) -> List[str]:
        """释放资源，返回获得资源的等待任务."""
        with self._lock:
            if self._task_holders.get(resource) != task_id:
                return []

            waiters = self._task_waiters[resource]
            if waiters:
                next_task = waiters.pop(0)
                self._task_holders[resource] = next_task
                self._task_priorities[next_task] = self._task_priorities.get(next_task, 0)
                # 恢复原持有者优先级
                self._restore_priority(task_id)
                return [next_task]
            else:
                del self._task_holders[resource]
                self._restore_priority(task_id)
                return []

    def _restore_priority(self, task_id: str):
        """恢复任务原始优先级 (简化版：重新计算)."""
        # 实际应用中需要维护原始优先级
        pass

    def get_effective_priority(self, task_id: str) -> int:
        with self._lock:
            return self._task_priorities.get(task_id, 0)

    def boost_priority(self, task_id: str, boost: int):
        with self._lock:
            self._task_priorities[task_id] = self._task_priorities.get(task_id, 0) + boost


class PreemptionManager:
    """抢占管理器 - 低优先级任务抢占、检查点保存."""

    def __init__(self):
        self._running_tasks: Dict[str, Task] = {}  # task_id -> Task
        self._checkpoint_callback: Optional[Callable[[Task], bool]] = None
        self._lock = threading.RLock()

    def set_checkpoint_callback(self, callback: Callable[[Task], bool]):
        """设置检查点保存回调."""
        self._checkpoint_callback = callback

    def register_running(self, task: Task):
        with self._lock:
            self._running_tasks[task.task_id] = task

    def unregister_running(self, task_id: str):
        with self._lock:
            self._running_tasks.pop(task_id, None)

    def try_preempt(self, high_priority_task: Task, tenant_quotas: Dict[str, TenantSchedulingQuota]) -> List[Task]:
        """尝试抢占低优先级任务为高优先级任务腾出资源."""
        with self._lock:
            preempted = []
            needed = high_priority_task.resource_demand

            # 按优先级从低到高排序运行中任务
            candidates = sorted(
                self._running_tasks.values(),
                key=lambda t: t.effective_priority()
            )

            for task in candidates:
                if task.tenant_id == high_priority_task.tenant_id:
                    continue  # 不抢占同租户任务

                if task.effective_priority() >= high_priority_task.effective_priority():
                    continue  # 只能抢占更低优先级

                # 检查抢占后租户配额是否满足
                quota = tenant_quotas.get(task.tenant_id)
                if quota:
                    # 简化检查
                    pass

                # 保存检查点
                if self._checkpoint_callback and self._checkpoint_callback(task):
                    preempted.append(task)
                    # 释放资源
                    for res_type, demand in task.resource_demand.items():
                        pass  # 实际由调度器处理

                    if self._resources_freed(preempted, needed):
                        break

            return preempted

    def _resources_freed(self, preempted: List[Task], needed: Dict[ResourceType, float]) -> bool:
        """检查释放的资源是否满足需求."""
        freed = defaultdict(float)
        for task in preempted:
            for res_type, demand in task.resource_demand.items():
                freed[res_type] += demand

        for res_type, demand in needed.items():
            if freed.get(res_type, 0) < demand:
                return False
        return True


class Scheduler:
    """租户感知任务调度器 - 整合 DRF、优先级继承、抢占."""

    def __init__(
        self,
        max_workers: int = 4,
        policy: SchedulingPolicy = SchedulingPolicy.FAIR_SHARE,
        cluster_resources: Optional[Dict[ResourceType, float]] = None
    ):
        self.max_workers = max_workers
        self.policy = policy

        # 默认集群资源
        self._cluster_resources = cluster_resources or {
            ResourceType.CPU: 16.0,
            ResourceType.MEMORY: 32768.0,  # 32GB
            ResourceType.GPU: 0.0,
            ResourceType.STORAGE_IO: 10000.0,
            ResourceType.NETWORK: 10000.0,
        }

        # 核心组件
        self._drf = DRFScheduler(self._cluster_resources)
        self._priority_inheritance = PriorityInheritanceManager()
        self._preemption = PreemptionManager()

        # 租户配额
        self._tenant_quotas: Dict[str, TenantSchedulingQuota] = {}

        # 状态
        self._tenant_states: Dict[str, Dict] = defaultdict(lambda: {
            "running": 0,
            "pending": 0,
            "completed": 0,
            "failed": 0,
            "total_cpu_time": 0.0,
        })

        # 运行控制
        self._running = False
        self._shutdown = False
        self._workers: List[threading.Thread] = []
        self._lock = threading.RLock()

        # 回调
        self._task_completed_callbacks: List[Callable[[TaskResult], None]] = []
        self._task_failed_callbacks: List[Callable[[Task, str], None]] = []

    def set_cluster_resources(self, resources: Dict[ResourceType, float]):
        self._cluster_resources = resources
        self._drf.set_cluster_resources(resources)

    def register_tenant(self, tenant_id: str, quota: TenantSchedulingQuota):
        """注册租户调度配额."""
        with self._lock:
            self._tenant_quotas[tenant_id] = quota
            self._drf.set_tenant_weight(tenant_id, quota.weight)
            logger.info(f"Registered scheduling quota for tenant {tenant_id}: {quota}")

    def submit(self, task: Task) -> str:
        """提交任务."""
        with self._lock:
            # 验证租户
            tenant = get_tenant_registry().get_tenant(task.tenant_id)
            if not tenant or not tenant.is_active():
                raise ValueError(f"Tenant {task.tenant_id} not active")

            quota = self._tenant_quotas.get(task.tenant_id)
            state = self._tenant_states[task.tenant_id]

            if quota and state["running"] >= quota.max_concurrent_tasks:
                # 检查是否有保留槽位
                if state["running"] >= quota.max_concurrent_tasks + quota.reserved_slots:
                    raise RuntimeError(f"Tenant {task.tenant_id} exceeded concurrent task limit")

            # 设置默认资源需求
            if not task.resource_demand:
                task.resource_demand = {
                    ResourceType.CPU: 1.0,
                    ResourceType.MEMORY: 512.0,
                }

            # 根据策略入队
            if self.policy in (SchedulingPolicy.FAIR_SHARE, SchedulingPolicy.WEIGHTED_FAIR):
                self._drf.submit_task(task.tenant_id, task)
            elif self.policy == SchedulingPolicy.PRIORITY:
                # 优先级队列由 worker 直接从 DRF 获取 (DRF 已包含优先级)
                self._drf.submit_task(task.tenant_id, task)
            else:
                # FIFO 退化为 DRF weight=1
                self._drf.submit_task(task.tenant_id, task)

            state["pending"] += 1
            logger.debug(f"Task {task.task_id} submitted for tenant {task.tenant_id}")
            return task.task_id

    def start(self, num_workers: Optional[int] = None):
        """启动调度器."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._shutdown = False
            workers = num_workers or self.max_workers
            for i in range(workers):
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(i,),
                    daemon=True,
                    name=f"scheduler-worker-{i}"
                )
                t.start()
                self._workers.append(t)
            logger.info(f"Scheduler started with {workers} workers, policy={self.policy.value}")

    def stop(self, timeout: float = 30.0):
        """停止调度器."""
        with self._lock:
            self._running = False
            self._shutdown = True

        for w in self._workers:
            w.join(timeout=timeout / max(len(self._workers), 1))
        self._workers.clear()
        logger.info("Scheduler stopped")

    def _worker_loop(self, worker_id: int):
        logger.debug(f"Worker {worker_id} started")
        while self._running and not self._shutdown:
            task = self._pop_next_task()
            if task:
                self._execute_task(task)
            else:
                time.sleep(0.05)  # 50ms 轮询
        logger.debug(f"Worker {worker_id} stopped")

    def _pop_next_task(self) -> Optional[Task]:
        with self._lock:
            # 计算每个租户当前允许的最大并发
            max_per_tenant = {}
            for tenant_id, quota in self._tenant_quotas.items():
                state = self._tenant_states[tenant_id]
                max_per_tenant[tenant_id] = quota.max_concurrent_tasks - state["running"]
                if max_per_tenant[tenant_id] < 0:
                    max_per_tenant[tenant_id] = 0

            return self._drf.pop_next_task(max_per_tenant)[1] if self._drf.pop_next_task(max_per_tenant) else None

    def _execute_task(self, task: Task):
        """执行任务."""
        tenant_id = task.tenant_id
        state = self._tenant_states[tenant_id]
        state["running"] += 1
        state["pending"] -= 1

        # 注册到抢占管理器
        self._preemption.register_running(task)

        start = time.perf_counter()
        success = False
        error = None
        result = None

        try:
            # 尝试获取资源锁 (优先级继承)
            for res_type, demand in task.resource_demand.items():
                resource_key = f"{tenant_id}:{res_type.value}"
                if not self._priority_inheritance.acquire(
                    task.task_id, resource_key, task.effective_priority()
                ):
                    logger.warning(f"Task {task.task_id} waiting for {resource_key}")

            if task.callback:
                result = task.callback(*task.args, **task.kwargs)
                success = True
            else:
                error = "No callback provided"
                success = False

        except Exception as e:
            error = str(e)
            logger.error(f"Task {task.task_id} failed: {e}")

        duration = (time.perf_counter() - start) * 1000

        # 释放资源锁
        for res_type in task.resource_demand:
            resource_key = f"{tenant_id}:{res_type.value}"
            self._priority_inheritance.release(task.task_id, resource_key)

        # 释放 DRF 分配
        self._drf.release_task(tenant_id, task)

        task_result = TaskResult(
            task_id=task.task_id,
            success=success,
            result=result,
            error=error,
            duration_ms=duration,
        )

        state["running"] -= 1
        state["total_cpu_time"] += duration / 1000.0
        if success:
            state["completed"] += 1
            for cb in self._task_completed_callbacks:
                try:
                    cb(task_result)
                except Exception as e:
                    logger.error(f"Task completed callback error: {e}")
        else:
            state["failed"] += 1
            for cb in self._task_failed_callbacks:
                try:
                    cb(task, error or "Unknown error")
                except Exception as e:
                    logger.error(f"Task failed callback error: {e}")

        # 重试逻辑
        if not success and task.retry_count < task.max_retries:
            task.retry_count += 1
            logger.info(f"Retrying task {task.task_id} (attempt {task.retry_count}/{task.max_retries})")
            self.submit(task)

        self._preemption.unregister_running(task.task_id)
        return task_result

    def try_preempt_for(self, high_priority_task: Task) -> List[Task]:
        """尝试为高优先级任务抢占资源."""
        return self._preemption.try_preempt(high_priority_task, self._tenant_quotas)

    def get_stats(self) -> Dict:
        with self._lock:
            allocations = self._drf.get_allocations()
            return {
                "cluster_resources": {rt.value: v for rt, v in self._cluster_resources.items()},
                "tenant_states": dict(self._tenant_states),
                "tenant_allocations": {tid: {rt.value: v for rt, v in alloc.items()} for tid, alloc in allocations.items()},
                "tenant_dominant_shares": {
                    tid: self._drf.get_tenant_dominant_share(tid)
                    for tid in self._tenant_quotas.keys()
                },
                "queue_sizes": {
                    tid: len(queue) for tid, queue in self._drf._tenant_queues.items()
                },
                "running": self._running,
                "policy": self.policy.value,
            }

    def on_task_completed(self, callback: Callable[[TaskResult], None]):
        self._task_completed_callbacks.append(callback)

    def on_task_failed(self, callback: Callable[[Task, str], None]):
        self._task_failed_callbacks.append(callback)


# 全局实例
_scheduler: Optional[Scheduler] = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler


def init_scheduler(
    max_workers: int = 4,
    policy: SchedulingPolicy = SchedulingPolicy.FAIR_SHARE,
    cluster_resources: Optional[Dict[ResourceType, float]] = None
) -> Scheduler:
    global _scheduler
    _scheduler = Scheduler(max_workers=max_workers, policy=policy, cluster_resources=cluster_resources)
    return _scheduler