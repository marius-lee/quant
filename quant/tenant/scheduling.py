"""资源调度与公平队列 - 租户感知调度、优先级继承、抢占."""

from __future__ import annotations
import heapq
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    FIFO = "fifo"                    # 先进先出
    PRIORITY = "priority"            # 优先级优先
    FAIR_SHARE = "fair_share"        # 公平份额 (DRF)
    WEIGHTED_FAIR = "weighted_fair"  # 加权公平


@dataclass
class Task:
    """调度任务."""
    task_id: str
    tenant_id: str
    priority: TaskPriority = TaskPriority.NORMAL
    submitted_at: datetime = field(default_factory=datetime.utcnow)
    deadline: Optional[datetime] = None
    estimated_duration: float = 0.0  # 预估执行时间(秒)
    callback: Optional[Callable] = None
    args: tuple = field(default_factory=tuple)
    kwargs: Dict = field(default_factory=dict)
    retry_count: int = 0
    max_retries: int = 3
    priority_boost: int = 0  # 优先级提升(用于优先级继承)

    def effective_priority(self) -> int:
        """计算有效优先级 (基础优先级 + 提升)."""
        return self.priority.value + self.priority_boost


@dataclass
class TenantQuota:
    """租户调度配额."""
    tenant_id: str
    max_concurrent_tasks: int = 4      # 最大并发任务数
    max_cpu_percent: float = 25.0      # 最大 CPU 使用率
    max_memory_mb: int = 2048          # 最大内存 MB
    priority_weight: float = 1.0       # 权重 (用于加权公平调度)
    reserved_slots: int = 0            # 保留槽位数


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


class FairQueue:
    """公平队列 - 基于 DRF (Dominant Resource Fairness) 算法."""

    def __init__(self, num_resources: int = 2):
        self._queues: Dict[str, List[Tuple[float, int, Task]]] = defaultdict(list)  # tenant -> [(dominant_share, seq, task)]
        self._tenant_shares: Dict[str, Dict[str, float]] = defaultdict(dict)  # tenant -> {resource: share}
        self._seq = 0
        self._lock = threading.Lock()

    def submit(self, tenant_id: str, task: Task, dominant_share: float) -> int:
        """提交任务."""
        with self._lock:
            self._seq += 1
            seq = self._seq
            heapq.heappush(self._queues[tenant_id], (dominant_share, seq, task))
            return seq

    def pop_next(self, available_resources: Dict[str, float]) -> Optional[Tuple[str, Task]]:
        """弹出下一个可运行任务 (DRF 算法)."""
        with self._lock:
            best_tenant = None
            best_task = None
            min_dominant_share = float('inf')

            for tenant_id, queue in self._queues.items():
                if not queue:
                    continue

                # 计算主导资源份额
                tenant_shares = self._tenant_shares.get(tenant_id, {})
                dominant = max(
                    (tenant_shares.get(res, 0) / max(avail, 1e-9))
                    for res, avail in available_resources.items()
                ) if available_resources else 0

                # 查看队首任务
                _, seq, task = queue[0]
                effective_priority = task.effective_priority()

                # DRF: 优先选择 dominant_share 最小的租户
                # 同分时按优先级
                score = (dominant, -task.effective_priority())
                if score < (min_dominant_share, float('inf')):
                    min_dominant_share = dominant
                    best_tenant = tenant_id
                    best_task = task

            if best_tenant:
                heapq.heappop(self._queues[best_tenant])
                return best_tenant, best_task

            return None

    def rebalance_shares(self, tenant_quotas: Dict[str, Dict[ResourceType, float]]):
        """根据租户配额重新计算份额."""
        with self._lock:
            for tenant_id, quotas in tenant_quotas.items():
                shares = {}
                for res_type, quota in quotas.items():
                    if quota.hard_limit > 0:
                        shares[res_type.value] = quota.hard_limit
                self._tenant_shares[tenant_id] = shares


class PriorityQueue:
    """优先级队列 - 支持优先级继承."""

    def __init__(self):
        self._queues: Dict[int, List[Tuple[int, Task]]] = defaultdict(list)
        self._lock = threading.Lock()

    def push(self, task: Task):
        with self._lock:
            effective = task.effective_priority()
            heapq.heappush(self._queues[effective], (time.time(), task))

    def pop(self) -> Optional[Task]:
        with self._lock:
            for priority in sorted(self._queues.keys(), reverse=True):
                if self._queues[priority]:
                    _, task = heapq.heappop(self._queues[priority])
                    return task
        return None

    def peek(self) -> Optional[Task]:
        with self._lock:
            for priority in sorted(self._queues.keys(), reverse=True):
                if self._queues[priority]:
                    return self._queues[priority][0][1]
        return None

    def boost_priority(self, task_id: str, boost: int):
        """优先级继承: 提升指定任务优先级."""
        with self._lock:
            for priority in self._queues:
                for i, (_, task) in enumerate(self._queues[priority]):
                    if task.task_id == task_id:
                        task.priority_boost += boost
                        # 重新入队
                        self._queues[priority].pop(i)
                        self.push(task)
                        return True
        return False


class Scheduler:
    """租户感知任务调度器."""

    def __init__(self, max_workers: int = 4, policy: SchedulingPolicy = SchedulingPolicy.FAIR_SHARE):
        self.max_workers = max_workers
        self.policy = policy
        self._workers: List[threading.Thread] = []
        self._running = False
        self._lock = threading.Lock()

        # 队列
        self._fair_queue = FairQueue()
        self._priority_queue = PriorityQueue()
        self._fifo_queue: List[Tuple[float, Task]] = []  # (submit_time, task)

        # 租户配额
        self._tenant_quotas: Dict[str, TenantQuota] = {}
        self._tenant_states: Dict[str, Dict] = defaultdict(lambda: {
            "running": 0,
            "pending": 0,
            "completed": 0,
            "failed": 0,
            "cpu_time": 0.0,
        })

        # 运行状态
        self._running = False
        self._workers: List[threading.Thread] = []
        self._lock = threading.Lock()
        self._shutdown = False

    def register_tenant(self, tenant_id: str, quota: TenantQuota):
        """注册租户配额."""
        self._tenant_quotas[tenant_id] = quota
        logger.info(f"Registered tenant quota: {tenant_id}")

    def submit(self, task: Task) -> str:
        """提交任务."""
        with self._lock:
            tenant = get_tenant_registry().get_tenant(task.tenant_id)
            if not tenant or not tenant.is_active():
                raise ValueError(f"Tenant {task.tenant_id} not active")

            # 检查配额
            quota = self._tenant_quotas.get(task.tenant_id)
            state = self._tenant_states[task.tenant_id]
            if quota and state["running"] >= quota.max_concurrent_tasks:
                raise RuntimeError(f"Tenant {task.tenant_id} exceeded concurrent task limit")

            # 选择队列
            if self.policy == SchedulingPolicy.FAIR_SHARE:
                dominant_share = self._calculate_dominant_share(task.tenant_id)
                self._fair_queue.submit(task.tenant_id, task, dominant_share)
            elif self.policy == SchedulingPolicy.PRIORITY:
                self._priority_queue.push(task)
            else:
                heapq.heappush(self._fifo_queue, (time.time(), task))

            self._tenant_states[task.tenant_id]["pending"] += 1
            return task.task_id

    def _calculate_dominant_share(self, tenant_id: str) -> float:
        """计算租户主导资源份额 (DRF)."""
        quota = self._tenant_quotas.get(tenant_id)
        if not quota:
            return 0.0

        # 简化：基于 CPU 和内存配额计算
        # 实际应结合集群总资源
        return max(
            quota.max_cpu_percent / 100.0,
            quota.max_memory_mb / 8192.0,  # 假设总内存 8GB
        )

    def start(self, num_workers: Optional[int] = None):
        """启动调度器."""
        if self._running:
            return
        self._running = True
        workers = num_workers or self.max_workers
        for i in range(workers):
            t = threading.Thread(target=self._worker_loop, args=(i,), daemon=True, name=f"scheduler-worker-{i}")
            t.start()
            self._workers.append(t)
        logger.info(f"Scheduler started with {workers} workers")

    def stop(self):
        """停止调度器."""
        self._running = False
        self._shutdown = True
        for w in self._workers:
            w.join(timeout=10)
        logger.info("Scheduler stopped")

    def _worker_loop(self, worker_id: int):
        while self._running and not self._shutdown:
            task = self._pop_next_task()
            if task:
                self._execute_task(task)
            else:
                time.sleep(0.1)

    def _pop_next_task(self) -> Optional[Task]:
        with self._lock:
            if self.policy == SchedulingPolicy.FAIR_SHARE:
                # 简化: 这里需要实际的可用资源
                return self._fair_queue.pop_next({"cpu": 100, "memory": 8192})[1] if self._fair_queue._queues else None
            elif self.policy == SchedulingPolicy.PRIORITY:
                return self._priority_queue.pop()
            else:
                if self._fifo_queue:
                    _, task = heapq.heappop(self._fifo_queue)
                    return task
        return None

    def _execute_task(self, task: Task):
        """执行任务."""
        tenant_id = task.tenant_id
        state = self._tenant_states[task.tenant_id]
        state["running"] += 1
        state["pending"] -= 1

        start = time.perf_counter()
        success = False
        error = None
        result = None

        try:
            if task.callback:
                result = task.callback(*task.args, **task.kwargs)
                success = True
            else:
                success = False
                error = "No callback"
        except Exception as e:
            error = str(e)
            logger.error(f"Task {task.task_id} failed: {e}")

        duration = (time.perf_counter() - start) * 1000

        result = TaskResult(
            task_id=task.task_id,
            success=success,
            result=result,
            error=error,
            duration_ms=duration,
        )

        state["running"] -= 1
        if success:
            state["completed"] += 1
        else:
            state["failed"] += 1

        # 重试逻辑
        if not success and task.retry_count < task.max_retries:
            task.retry_count += 1
            self.submit(task)

        return result

    def get_stats(self) -> Dict:
        with self._lock:
            return {
                "tenant_states": dict(self._tenant_states),
                "queue_sizes": {
                    "fair": sum(len(q) for q in self._fair_queue._queues.values()),
                    "priority": sum(len(q) for q in self._priority_queue._queues.values()),
                    "fifo": len(self._fifo_queue),
                },
                "running": self._running,
            }


# 全局实例
_scheduler: Optional[Scheduler] = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler


def init_scheduler(max_workers: int = 4, policy: SchedulingPolicy = SchedulingPolicy.FAIR_SHARE) -> Scheduler:
    global _scheduler
    _scheduler = Scheduler(max_workers=max_workers, policy=policy)
    return _scheduler