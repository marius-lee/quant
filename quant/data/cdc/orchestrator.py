"""CDC 同步编排器 — 多表依赖感知同步编排."""

from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Set, Optional, Callable
from collections import defaultdict, deque

from quant.data.cdc.syncer import IncrementalSyncer, SyncConfig, SyncResult
from quant.data.cdc.schema_evolution import SchemaEvolutionManager, get_schema_manager
from quant.data.cdc.capture import get_capture
from quant.utils.logger import get_logger
from quant.config.paths import MARKET_DB
from quant.config.constants import _require_cfg

logger = get_logger("data.cdc.orchestrator")


@dataclass
class TableDependency:
    """表依赖关系."""
    table: str
    depends_on: Set[str]  # 依赖的表
    depends_by: Set[str]  # 被依赖的表


@dataclass
class SyncPlan:
    """同步计划."""
    batches: List[List[str]]  # 按依赖顺序分批的表列表
    parallel: bool = True


class CDCSyncerOrchestrator:
    """CDC 同步编排器.

    功能:
    1. 管理多表同步的依赖顺序
    2. 并行/串行同步控制
    3. 失败重试与熔断
    4. 进度监控与指标收集
    """

    # 表依赖关系 (基于 FK 和业务逻辑)
    DEFAULT_DEPENDENCIES = {
        "daily": set(),  # 基础表，无依赖
        "daily_valuation": {"daily"},  # 依赖 daily
        "fund_flow": {"daily"},
        "margin_detail": {"daily"},
        "lhb_detail": {"daily"},
        "limit_up_pool": {"daily"},
        "limit_down_pool": {"daily"},
        "adj_factor": {"daily"},
        "stocks": set(),  # 基础表
        "dividend": {"stocks"},
        "fund_hold": {"stocks"},
        "holder_trade": {"stocks"},
        "pledge": {"stocks"},
        "index_daily": set(),
    }

    def __init__(
        self,
        syncer_config: Optional[Dict] = None,
        max_parallel: int = 4,
        enable_schema_sync: bool = True,
    ):
        self.max_parallel = max_parallel
        self.enable_schema_sync = enable_schema_sync

        # 初始化组件
        sync_config = SyncConfig(**(syncer_config or {}))
        self.syncer = IncrementalSyncer(SyncConfig(**(syncer_config or {})))
        self.schema_manager = get_schema_manager()

        # 状态
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._sync_plan: Optional[SyncPlan] = None
        self._stats = {
            "total_synced": 0,
            "total_failed": 0,
            "last_sync_time": None,
        }
        self._lock = threading.Lock()

        # 初始化同步计划
        self._build_sync_plan()

    def _build_sync_plan(self):
        """构建同步计划 (拓扑排序)."""
        deps = self.DEFAULT_DEPENDENCIES

        # Kahn 算法拓扑排序
        in_degree = {table: len(deps) for table, deps in deps.items()}
        # 添加没有依赖也没被依赖的表
        for table in deps:
            if table not in in_degree:
                in_degree[table] = 0

        queue = deque([t for t, d in in_degree.items() if d == 0])
        batches = []
        current_batch = []

        while queue:
            table = queue.popleft()
            current_batch.append(table)

            for dependent in self.DEFAULT_DEPENDENCIES.get(table, set()):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

            # 批次大小控制
            if len(current_batch) >= 4:
                batches.append(current_batch)
                current_batch = []

        if current_batch:
            batches.append(current_batch)

        self._sync_plan = SyncPlan(batches=batches)
        logger.info(f"Sync plan built: {len(batches)} batches, {sum(len(b) for b in batches)} tables")

    def start(self):
        """启动编排器."""
        if self._running:
            return

        # 启动同步器
        self.syncer.start()

        # 启动 Schema 同步 (如果启用)
        if self.enable_schema_sync:
            self._start_schema_sync()

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="cdc-orchestrator")
        self._thread.start()
        logger.info("CDC Orchestrator started")

    def stop(self):
        """停止编排器."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        self.syncer.stop()
        logger.info("CDC Orchestrator stopped")

    def _run_loop(self):
        """主循环: 按批次执行同步."""
        while self._running:
            try:
                self._execute_sync_batch()
            except Exception as e:
                logger.error(f"Orchestrator loop error: {e}")
            time.sleep(5)  # 批次间隔

    def _execute_sync_batch(self):
        """执行一批同步."""
        if not self._sync_plan or not self._sync_plan.batches:
            return

        for batch in self._sync_plan.batches:
            if not self._running:
                break

            logger.info(f"Executing sync batch: {batch}")

            if len(batch) == 1:
                # 单表同步
                table = batch[0]
                result = self.syncer.sync_table_full(batch[0])
                self._update_stats(result)
            else:
                # 并行同步批次
                self._sync_batch_parallel(batch)

    def _sync_batch_parallel(self, tables: List[str]):
        """并行同步一批表."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=min(self.max_parallel, len(tables))) as executor:
            futures = {executor.submit(self.syncer.sync_table_full, table): table for table in tables}

            for future in as_completed(futures):
                table = futures[future]
                try:
                    result = future.result()
                    self._update_stats(result)
                except Exception as e:
                    logger.error(f"Sync failed for {table}: {e}")
                    self._stats["total_failed"] += 1

    def _update_stats(self, result: SyncResult):
        with self._lock:
            if result.success:
                self._stats["total_synced"] += result.events_processed
            else:
                self._stats["total_failed"] += 1
            self._stats["last_sync_time"] = time.time()

    def _start_schema_sync(self):
        """启动 Schema 同步后台线程."""
        def schema_sync_loop():
            while self._running:
                try:
                    results = self.schema_manager.sync_all()
                    failed = [t for t, ok in results.items() if not ok]
                    if failed:
                        logger.warning(f"Schema sync failed for: {failed}")
                except Exception as e:
                    logger.error(f"Schema sync error: {e}")
                time.sleep(300)  # 5分钟检查一次

        threading.Thread(target=schema_sync_loop, daemon=True, name="schema-sync").start()

    def get_stats(self) -> Dict:
        """获取同步统计."""
        with self._lock:
            return dict(self._stats)

    def get_sync_plan(self) -> Optional[SyncPlan]:
        return self._sync_plan

    def run_full_sync(self) -> Dict[str, SyncResult]:
        """执行全量同步 (用于初始化/修复)."""
        results = {}
        for batch in self._sync_plan.batches:
            for table in batch:
                result = self.syncer.sync_table_full(batch[0])
                self._update_stats(result)
                results[table] = result
        return results

    def sync_table_now(self, table: str) -> SyncResult:
        """立即同步单表."""
        return self.syncer.sync_table_full(table)

    def get_stats(self) -> Dict:
        return dict(self._stats)