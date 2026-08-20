"""分布式因子计算引擎核心 — 基于 Ray Task/Actor.

架构:
  1. Partitioner 将工作拆分为 Partition (日期批次 × 因子批次)
  2. 每个 Partition 提交为一个 Ray Task (或 Actor 任务)
  3. Task 内部调用现有 FactorStore.materialize 逻辑 (复用单进程计算)
  4. 结果通过 FactorResultAggregator 合并写入缓存
  5. 支持精确一次语义: 幂等写入 + 任务级重试
"""

import ray
import time
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Callable
from concurrent.futures import ThreadPoolExecutor
from quant.factor.distributed.partitioner import Partition, create_partitioner
from quant.factor.distributed.ray_config import init_ray, shutdown_ray, factor_task, get_actor_pool, auto_select_partition_strategy, start_memory_monitor, stop_memory_monitor
from quant.factor.distributed.aggregator import FactorResultAggregator, ComputeResult
from quant.utils.logger import get_logger

logger = get_logger("factor.distributed.engine")


@dataclass
class FactorComputeTask:
    """因子计算任务描述."""
    partition: Partition
    factor_store_config: dict  # FactorStore 初始化参数
    compute_func: str          # 计算函数路径 (如 "quant.factor.store:FactorStore.materialize")
    priority: int = 0          # 调度优先级


class DistributedFactorEngine:
    """分布式因子计算引擎.

    用法:
        engine = DistributedFactorEngine(
            start_date="2020-01-01",
            end_date="2024-12-31",
            partition_strategy="date",  # 或 "factor", "symbol", "composite"
        )
        engine.run()
    """

    def __init__(
        self,
        start_date: str,
        end_date: str,
        factors: Optional[List[str]] = None,
        symbols: Optional[List[str]] = None,
        partition_strategy: str = "date",
        partition_kwargs: Optional[Dict] = None,
        ray_config: Optional[Dict] = None,
        max_concurrent_tasks: int = 0,  # 0 = 无限制 (受 Ray 资源限制)
        use_actor_pool: bool = False,  # 是否使用 Actor 池复用 DB 连接
        actor_pool_size: int = 0,  # Actor 池大小 (0=自动, 根据 CPU 核心数)
        incremental: bool = True,  # 是否启用增量物化 (仅物化新增日期)
        quarantine_failed_factors: bool = True,  # 是否隔离失败因子
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.factors = factors or []
        self.symbols = symbols or []
        self.partition_strategy = partition_strategy
        self.partition_kwargs = partition_kwargs or {}
        self.ray_config = ray_config or {}
        self.max_concurrent_tasks = max_concurrent_tasks
        self.use_actor_pool = use_actor_pool
        self.actor_pool_size = actor_pool_size
        self.incremental = incremental
        self.quarantine_failed_factors = quarantine_failed_factors

        # 运行时状态
        self._partitions: List[Partition] = []
        self._aggregator = FactorResultAggregator()
        self._ray_initialized = False
        self._actor_pool = None
        self._quarantined_factors: set = set()  # 隔离的失败因子

    def prepare(self) -> List[Partition]:
        """准备分区 (不启动 Ray)."""
        if not self.factors:
            from quant.factor.compute import get_factor_names
            self.factors = sorted(set(get_factor_names(status_filter='backtesting'))
                                  | set(get_factor_names(status_filter='using')))
            logger.info(f"Auto-discovered {len(self.factors)} factors")

        if not self.symbols:
            from quant.data.repos.universe_repo import UniverseRepo
            self.symbols = UniverseRepo().get_symbols(exclude_market='BJ')
            logger.info(f"Auto-discovered {len(self.symbols)} symbols (excl. BJ)")

        # 自动选择分区策略 (如果用户未显式指定 partition_kwargs 中的策略相关参数)
        if not self.partition_kwargs.get('strategy_locked', False):
            from quant.factor.distributed.ray_config import auto_select_partition_strategy
            import ray
            cluster_cpus = ray.available_resources().get('CPU', 1) if ray.is_initialized() else 1
            strategy, kwargs = auto_select_partition_strategy(
                num_factors=len(self.factors),
                num_symbols=len(self.symbols),
                num_dates=len(self.get_trading_dates()),
                cluster_cpus=int(cluster_cpus),
            )
            if strategy != self.partition_strategy:
                logger.info(f"Auto-selected partition strategy: {self.partition_strategy} -> {strategy}")
                self.partition_strategy = strategy
            self.partition_kwargs.update(kwargs)

        # 创建分区器
        partitioner = create_partitioner(
            self.partition_strategy,
            self.start_date,
            self.end_date,
            self.factors,
            self.symbols,
            **self.partition_kwargs,
        )
        self._partitions = partitioner.partition()
        # 增量模式: 过滤已缓存的分区
        self._partitions = self.filter_incremental_partitions(self._partitions)
        
        logger.info(f"Prepared {len(self._partitions)} partitions for {self.start_date} - {self.end_date}")

        # 初始化 Actor 池 (如果启用)
        if self.use_actor_pool and ray.is_initialized():
            from quant.factor.distributed.ray_config import get_actor_pool
            pool_size = self.actor_pool_size or max(1, len(self._partitions) // 2)
            self._actor_pool = get_actor_pool(
                pool_size=pool_size,
                factor_store_config={"db_path": "quant/data/factor_cache.db"},
            )
            logger.info(f"Actor pool initialized with {pool_size} actors")

        return self._partitions

    def get_trading_dates(self) -> List[str]:
        """获取交易日列表."""
        from quant.execution.calendar import get_trading_dates
        return get_trading_dates(self.start_date, self.end_date)

    def get_cached_dates(self) -> set:
        """获取已缓存的日期集合 (用于增量物化)."""
        from quant.factor.store import FactorStore
        fs = FactorStore(db_path="quant/data/factor_cache.db")
        try:
            cached = fs.get_cached_dates(self.factors[0] if self.factors else None)
            return set(cached) if cached else set()
        finally:
            fs.close()

    def filter_incremental_partitions(self, partitions: List[Partition]) -> List[Partition]:
        """增量模式: 过滤掉已缓存的日期分区."""
        if not self.incremental:
            return partitions
        
        cached_dates = self.get_cached_dates()
        if not cached_dates:
            return partitions
        
        filtered = []
        for p in partitions:
            new_dates = [d for d in p.dates if d not in cached_dates]
            if new_dates:
                filtered.append(Partition(
                    partition_id=p.partition_id,
                    dates=new_dates,
                    factors=p.factors,
                    symbols=p.symbols,
                    metadata=p.metadata,
                ))
        
        logger.info(f"Incremental mode: {len(partitions)} -> {len(filtered)} partitions "
                    f"({sum(len(p.dates) for p in partitions)} -> {sum(len(p.dates) for p in filtered)} dates)")
        return filtered

    def run(self) -> Dict[str, Any]:
        """执行分布式因子物化."""
        if not self._partitions:
            self.prepare()

        if not self._partitions:
            logger.warning("No partitions to compute")
            return {"status": "empty", "partitions": 0}

        # 初始化 Ray
        init_ray(self.ray_config)
        self._ray_initialized = True

        # 启动内存压力监控
        from quant.factor.distributed.ray_config import start_memory_monitor, stop_memory_monitor
        start_memory_monitor(
            system_memory_threshold=0.85,
            check_interval=10,
        )

        try:
            # 提交所有任务
            futures = self._submit_tasks()

            # 等待完成并收集结果
            self._collect_results(futures)

            # 生成汇总
            summary = self._aggregator.get_summary()
            summary["start_date"] = self.start_date
            summary["end_date"] = self.end_date
            summary["partition_strategy"] = self.partition_strategy
            summary["num_factors"] = len(self.factors)
            summary["num_symbols"] = len(self.symbols)

            logger.info(f"Distributed factor compute completed: {summary}")
            return summary

        finally:
            stop_memory_monitor()
            if self._ray_initialized:
                shutdown_ray()
                # 关闭 Actor 池
                from quant.factor.distributed.ray_config import shutdown_actor_pool
                shutdown_actor_pool()

    def _submit_tasks(self) -> List[ray.ObjectRef]:
        """提交所有分区计算任务到 Ray."""

        # 准备 FactorStore 配置 (可序列化)
        factor_store_config = {
            "db_path": "quant/data/factor_cache.db",
        }

        if self.use_actor_pool and self._actor_pool:
            # 使用 Actor 池模式
            @ray.remote(num_cpus=1, max_retries=3, retry_exceptions=True)
            def compute_partition_with_actor(partition: Partition, actor_pool_ref) -> ComputeResult:
                """Ray Task: 使用 Actor 池计算单个分区."""
                task_start = time.perf_counter()
                pid = partition.partition_id

                actor = None
                try:
                    # 从 Actor 池获取 Actor
                    actor = ray.get(actor_pool_ref.acquire.remote(timeout=30.0))
                    
                    # 执行物化
                    result = ray.get(actor.materialize.remote(
                        partition.dates,
                        partition.factors,
                        partition.symbols,
                        False,
                    ))

                    elapsed_ms = (time.perf_counter() - task_start) * 1000
                    rows = result.get("n_rows", 0)

                    return ComputeResult(
                        partition_id=pid,
                        success=True,
                        rows_written=rows,
                        elapsed_ms=elapsed_ms,
                        metadata={"dates": len(partition.dates), "factors": len(partition.factors)},
                    )

                except Exception as e:
                    elapsed_ms = (time.perf_counter() - task_start) * 1000
                    logger.error(f"Partition {pid} failed: {e}")
                    return ComputeResult(
                        partition_id=pid,
                        success=False,
                        elapsed_ms=elapsed_ms,
                        error=str(e),
                    )
                finally:
                    # 释放 Actor 回池
                    if actor:
                        actor_pool_ref.release.remote(actor)

            # 提交任务 - 传递 Actor pool 引用
            futures = []
            for partition in self._partitions:
                future = compute_partition_with_actor.remote(partition, self._actor_pool)
                futures.append(future)

                # 控制并发提交速率 (避免 OOM)
                if self.max_concurrent_tasks and len(futures) >= self.max_concurrent_tasks:
                    ready, futures = ray.wait(futures, num_returns=max(1, len(futures) // 2), timeout=60)
                    self._collect_results(ready)
                    futures = list(futures)

        else:
            # 原有模式: 每个 Task 创建独立 FactorStore
            @ray.remote(num_cpus=1, max_retries=3, retry_exceptions=True)
            def compute_partition(partition: Partition, factor_store_config: dict) -> ComputeResult:
                """Ray Task: 计算单个分区."""
                task_start = time.perf_counter()
                pid = partition.partition_id

                try:
                    # 延迟导入避免序列化问题
                    from quant.factor.store import FactorStore

                    # 创建 FactorStore (每个 Task 独立实例)
                    fs = FactorStore(**factor_store_config)

                    # 执行物化
                    result = fs.materialize(
                        dates=partition.dates,
                        factors=partition.factors,
                        symbols=partition.symbols,
                        force=False,
                    )

                    elapsed_ms = (time.perf_counter() - task_start) * 1000
                    rows = result.get("n_rows", 0)

                    fs.close()

                    return ComputeResult(
                        partition_id=pid,
                        success=True,
                        rows_written=rows,
                        elapsed_ms=elapsed_ms,
                        metadata={"dates": len(partition.dates), "factors": len(partition.factors)},
                    )

                except Exception as e:
                    elapsed_ms = (time.perf_counter() - task_start) * 1000
                    logger.error(f"Partition {pid} failed: {e}")
                    return ComputeResult(
                        partition_id=pid,
                        success=False,
                        elapsed_ms=elapsed_ms,
                        error=str(e),
                    )

            # 准备 FactorStore 配置 (可序列化)
            factor_store_config = {
                "db_path": "quant/data/factor_cache.db",
            }

            # 提交任务
            futures = []
            for partition in self._partitions:
                future = compute_partition.remote(partition, factor_store_config)
                futures.append(future)

                # 控制并发提交速率 (避免 OOM)
                if self.max_concurrent_tasks and len(futures) >= self.max_concurrent_tasks:
                    # 等待一部分完成
                    ready, futures = ray.wait(futures, num_returns=max(1, len(futures) // 2), timeout=60)
                    self._collect_results(ready)
                    futures = list(futures)  # 剩余未完成

    def _collect_results(self, futures: List[ray.ObjectRef]):
        """收集任务结果."""
        if not futures:
            return

        # 分批等待 (避免一次性 get 太多导致内存压力)
        batch_size = 50
        for i in range(0, len(futures), batch_size):
            batch = futures[i:i + batch_size]
            results = ray.get(batch)
            for result in results:
                self._aggregator.add_result(result)
                if result.success:
                    logger.info(f"Partition {result.partition_id} OK: {result.rows_written} rows, {result.elapsed_ms:.0f}ms")
                else:
                    logger.error(f"Partition {result.partition_id} FAILED: {result.error}")
                    # 隔离失败因子
                    if self.quarantine_failed_factors:
                        self._quarantine_failed_factors(result)

    def _quarantine_failed_factors(self, result: ComputeResult):
        """隔离失败的因子."""
        # 从 partition_id 或 metadata 中提取因子名称
        failed_factors = result.metadata.get("failed_factors", [])
        if not failed_factors:
            # 尝试从 partition_id 推断
            pid = result.partition_id
            if "_f" in pid:
                # composite_d{di}_f{fi} 格式
                try:
                    fi = int(pid.split("_f")[-1])
                    if fi < len(self.factors):
                        failed_factors = [self.factors[fi]]
                except (ValueError, IndexError):
                    pass
        
        for factor in failed_factors:
            if factor not in self._quarantined_factors:
                self._quarantined_factors.add(factor)
                logger.warning(f"Factor quarantined: {factor} (reason: {result.error})")
        
        # 持久化隔离列表
        self._save_quarantine_list()

    def _save_quarantine_list(self):
        """保存隔离列表到文件."""
        import json
        quarantine_file = "quant/data/factor_quarantine.json"
        try:
            with open(quarantine_file, 'w') as f:
                json.dump({
                    "quarantined_factors": list(self._quarantined_factors),
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save quarantine list: {e}")

    def load_quarantine_list(self):
        """加载隔离列表."""
        import json
        quarantine_file = "quant/data/factor_quarantine.json"
        try:
            with open(quarantine_file, 'r') as f:
                data = json.load(f)
                self._quarantined_factors = set(data.get("quarantined_factors", []))
                logger.info(f"Loaded {len(self._quarantined_factors)} quarantined factors")
        except FileNotFoundError:
            self._quarantined_factors = set()
        except Exception as e:
            logger.warning(f"Failed to load quarantine list: {e}")
            self._quarantined_factors = set()


# ════════════════════════════════════════════════════════════════════
# 便捷入口函数
# ═══════════════════════════════════════════════════════════════════

def run_distributed_factorization(
    start_date: str,
    end_date: str,
    factors: Optional[List[str]] = None,
    symbols: Optional[List[str]] = None,
    partition_strategy: str = "date",
    partition_kwargs: Optional[Dict] = None,
    ray_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """一键运行分布式因子物化.

    参数:
        start_date: 物化起始日期 (YYYY-MM-DD)
        end_date: 物化结束日期 (YYYY-MM-DD)
        factors: 因子列表 (None = 自动发现)
        symbols: 股票列表 (None = 自动全市场)
        partition_strategy: "date" | "factor" | "symbol" | "composite"
        partition_kwargs: 分区器参数 (如 max_partition_size, dates_per_partition)
        ray_config: Ray 配置 (mode, address, num_cpus 等)

    返回:
        汇总统计字典
    """
    engine = DistributedFactorEngine(
        start_date=start_date,
        end_date=end_date,
        factors=factors,
        symbols=symbols,
        partition_strategy=partition_strategy,
        partition_kwargs=partition_kwargs,
        ray_config=ray_config,
    )
    return engine.run()