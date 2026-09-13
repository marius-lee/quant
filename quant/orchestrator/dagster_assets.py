"""Dagster 资产/作业定义 — 替代自研 orchestrator.

设计:
  - 每个调度任务 = 一个 Dagster Asset/Op
  - 依赖通过 AssetIn/AssetOut 显式声明
  - 时间分区: DailyPartitionsDefinition (交易日) + WeeklyPartitionsDefinition (周六)
  - 资源: DataSourceRegistry, FactorStore, TradeRepo 等
  - 调度器: Dagster Daemon (cron) 替代自研 30s 轮询
  - 可观测: Dagster UI + 结构化日志 + 指标导出

修复历史 (v565-v577):
  v577 fix:
    - 所有 asset 函数添加 start_time = _time.perf_counter() (原全部引用未定义变量)
    - signals asset 调用 generate_signals() + 更新 broker state (与 legacy 一致)
    - 删除冗余 _dagster_log_start/_dagster_log_finish (module _run() 已调用 task_log)
    - xgb_train 调用 _run() (原遗漏)
    - adj_factor 避免与 module 函数同名混淆
    - monitor asset 在独立线程启动守护进程, 主线程立即返回
    - _start_dagster() 正确启动 Dagster daemon 进程

双模式架构:
  Legacy (默认): quant/scheduler/orchestrator.py 30s 轮询
  Dagster (QUANT_ORCHESTRATOR=dagster): 本文件 + Dagster Daemon
  Web UI 通过 task_runs 表统一监控, 不感知模式差异
"""
import time as _time
import os
import sys
import threading
import dagster as dg
from dagster import (
    asset,
    DefaultSensorStatus,
    AssetIn,
    AssetOut,
    AssetExecutionContext,
    DailyPartitionsDefinition,
    WeeklyPartitionsDefinition,
    define_asset_job,
    AssetSelection,
    ScheduleDefinition,
    SensorDefinition,
    DefaultScheduleStatus,
    RunRequest,
    SkipReason,
    ResourceParam,
    RetryPolicy,
    Backoff,
    Jitter,
)
from datetime import datetime, date, time as _dt_time, timedelta
from typing import Optional

# ═══════════════════════════════════════════════════════════════════
# 分区定义
# ═══════════════════════════════════════════════════════════════════

trading_day_partitions = DailyPartitionsDefinition(
    start_date="2020-01-01",
    end_offset=1,
    timezone="Asia/Shanghai",
)

weekly_partitions = WeeklyPartitionsDefinition(
    start_date="2024-01-06",
    end_offset=2,
    day_of_week=5,
    timezone="Asia/Shanghai",
)

# ═══════════════════════════════════════════════════════════════════
# 资源定义
# ═══════════════════════════════════════════════════════════════════

from dagster import EnvVar

from quant.utils.logger import get_logger

logger = get_logger("orchestrator.dagster_assets")



class DataSourceRegistryResource(dg.ConfigurableResource):
    state_dir: str = "/tmp/quant_sources"
    state_dir_env: Optional[str] = None

    def __post_init__(self):
        if self.state_dir_env:
            self.state_dir = EnvVar(self.state_dir_env).get_value()

    def get_client(self):
        from quant.data.sources.registry import get_registry
        registry = get_registry()
        registry.load_from_config()
        return registry


class FactorStoreResource(dg.ConfigurableResource):
    db_path: str = "quant/data/factor_cache.db"
    db_path_env: Optional[str] = None

    def __post_init__(self):
        if self.db_path_env:
            self.db_path = EnvVar(self.db_path_env).get_value()

    def get_client(self):
        from quant.factor.store import FactorStore
        return FactorStore(db_path=self.db_path)


class TradeRepoResource(dg.ConfigurableResource):
    db_path: str = "quant/data/trades.db"
    db_path_env: Optional[str] = None

    def __post_init__(self):
        if self.db_path_env:
            self.db_path = EnvVar(self.db_path_env).get_value()

    def get_client(self):
        from quant.data.repos import TradeRepo
        return TradeRepo(db_path=self.db_path)


class MarketDBResource(dg.ConfigurableResource):
    db_path: str = "quant/data/market.db"
    db_path_env: Optional[str] = None

    def __post_init__(self):
        if self.db_path_env:
            self.db_path = EnvVar(self.db_path_env).get_value()

    def get_client(self):
        import sqlite3
        from quant.config.constants import _require_cfg
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
        return conn


# ═══════════════════════════════════════════════════════════════════
# 重试策略
# ═══════════════════════════════════════════════════════════════════

RETRY_POLICY = RetryPolicy(
    max_retries=3,
    delay=10,
    backoff=Backoff.EXPONENTIAL,
    jitter=Jitter.PLUS_MINUS,
)

# ═══════════════════════════════════════════════════════════════════
# 工具函数: 统一 metadata 写入
# ═══════════════════════════════════════════════════════════════════


def _add_metadata(context, partition_date, status, **kwargs):
    context.add_output_metadata({
        "partition_date": partition_date,
        "status": status,
        **{k: v for k, v in kwargs.items() if v is not None},
    })


# ═══════════════════════════════════════════════════════════════════
# 资产定义
# 注意: module _run() 函数内部已调用 task_log.start/finish,
# 不需要额外包装 (避免双重写入 task_runs)
# ═══════════════════════════════════════════════════════════════════


@asset(
    description="早间补拉链 — 重试前3天审计失败表 + 7天未OK的weekly_full表 + factor_cache兜底",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    metadata={"owner": "data-engineering", "priority": "high"},
)
def daily_repair(
    context: AssetExecutionContext,
    market_db: MarketDBResource,
) -> dict:
    """每日 05:00 运行 (非交易日也运行, 覆盖周五晚间链缺口)."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] daily_repair starting")
    start_time = _time.perf_counter()

    from quant.scheduler.repair import _run as _repair_run
    from quant.scheduler.task_log import finish as _tk_finish
    # v625: repair._run() lacks outer try/finally; guard the row on crash.
    try:
        _repair_run(partition_date)
    except Exception as e:
        context.log.exception(f"[{partition_date}] daily_repair task failed")
        try:
            _tk_finish("daily_repair", partition_date, "failed", error=str(e))
        except Exception as _fe:
            context.log.warning(f"daily_repair finish-on-crash failed: {_fe}")
        raise

    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed", duration_ms=duration_ms)
    return {"date": partition_date, "status": "completed", "duration_ms": duration_ms}


@asset(
    description="信号生成 — 计算所有using因子, 生成Alpha信号与目标持仓",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    ins={"daily_repair": AssetIn("daily_repair")},
    metadata={"owner": "quant-research", "priority": "high"},
)
def signals(
    context: AssetExecutionContext,
    daily_repair: dict,
    factor_store: FactorStoreResource,
) -> dict:
    """每日 08:30 运行 — Dagster 模式下生成信号并写入 task_runs + broker.state.

    v577/v625 fix: delegate to signals._run() (V586-wrapped -> task_log
    start->ok/failed guaranteed in BOTH Dagster and Legacy modes), then sync
    broker.state (Dagster-only; Legacy updates broker via scheduler broadcast).

    Previously this asset INLINED generate_signals() AND had a premature
    `return` at the ok-path -> the broker.update() block + _tk_finish were
    DEAD CODE (unreachable) -> signals never wrote task_runs in Dagster mode and
    broker.state was never synced (HANDOFF v622/v624 regression).
    """
    # v594: signals 应使用当天日期, 而非 partition_date (前一天)
    from datetime import date as _date
    today = _date.today().isoformat()
    partition_date = context.partition_key
    context.log.info(f"[{today}] signals starting (partition={partition_date})")
    start_time = _time.perf_counter()

    from quant.scheduler.signals import _run as _signals_run
    # signals._run() is V586-wrapped: finishes ok/failed internally, propagates crash.
    result = _signals_run(today)

    # v622: Dagster-specific broker.state sync (Legacy syncs via scheduler broadcast)
    try:
        from quant.core.state_broker import broker
        from quant.data.repos import TradeRepo
        repo = TradeRepo()
        sig = repo.get_latest_signals()
        if sig and sig.get("targets"):
            broker.update({"signals": sig["targets"], "date": sig.get("date")})
    except Exception as _e:
        context.log.warning(f"signals bridge update failed: {_e}")

    targets = result.get("targets", 0) if result else 0
    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed",
                 targets=targets, duration_ms=duration_ms)
    return {"date": partition_date, "targets": targets, "duration_ms": duration_ms}


@asset(
    description="交易执行 — 读取信号、获取行情、执行调仓订单",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    ins={"signals": AssetIn("signals")},
    metadata={"owner": "execution", "priority": "high"},
)
def execute(
    context: AssetExecutionContext,
    signals: dict,
    trade_repo: TradeRepoResource,
) -> dict:
    """每日 09:20 运行 (仅调仓日)."""
    # v594: execute 应使用当天日期, 而非 partition_date (前一天)
    from datetime import date as _date
    today = _date.today().isoformat()
    partition_date = context.partition_key
    context.log.info(f"[{today}] execute starting (partition={partition_date})")
    start_time = _time.perf_counter()

    from quant.scheduler.execute import _run as _exec_run
    from quant.scheduler.task_log import finish as _tk_finish
    # v625: execute._run() manages task_log but lacks outer try/finally; guard the
    # row on crash so a mid-trade exception never leaves task_runs='running'.
    try:
        result = _exec_run(today)
    except Exception as e:
        context.log.exception(f"[{today}] execute task failed")
        try:
            _tk_finish("execute", today, "failed", error=str(e))
        except Exception as _fe:
            context.log.warning(f"execute finish-on-crash failed: {_fe}")
        raise
    result = result or {"sells": 0, "limit_buys": 0, "elapsed": 0}

    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed",
                 sells=result.get("sells", 0),
                 limit_buys=result.get("limit_buys", 0),
                 duration_ms=duration_ms)
    return {"date": partition_date,
            "sells": result.get("sells", 0),
            "limit_buys": result.get("limit_buys", 0),
            "duration_ms": duration_ms}


@asset(
    description="开盘快照 — 快照所有A股开盘30分钟实时价+量",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    ins={"execute": AssetIn("execute")},
    metadata={"owner": "data-engineering"},
)
def snapshot_open(
    context: AssetExecutionContext,
    execute: dict,
    market_db: MarketDBResource,
) -> dict:
    """每日 10:00 运行."""
    # v594: snapshot_open 应使用当天日期, 而非 partition_date (前一天)
    from datetime import date as _date
    today = _date.today().isoformat()
    partition_date = context.partition_key
    context.log.info(f"[{today}] snapshot_open starting (partition={partition_date})")
    start_time = _time.perf_counter()

    from quant.scheduler.snapshot import snapshot_open as _snapshot_open
    result = _snapshot_open(today)

    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed",
                 saved=result.get("saved", 0),
                 errors=result.get("errors", 0),
                 duration_ms=duration_ms)
    return {"date": partition_date,
            "saved": result.get("saved", 0),
            "duration_ms": duration_ms}


@asset(
    description="盘中风控 — 每30秒轮询 止损/止盈/熔断, 触发后立即卖出",
    partitions_def=trading_day_partitions,
    kinds={"python", "monitoring"},
    metadata={"owner": "risk", "priority": "critical"},
)
def monitor(
    context: AssetExecutionContext,
    market_db: MarketDBResource,
) -> dict:
    """09:35-15:00 持续运行 (午休内部暂停). 启动后台守护进程后立即返回.

    v577 fix: Dagster Asset 执行不能长时间阻塞, 守护进程在独立线程运行,
    主线程立即返回让 Asset 标记完成. Sensor 控制生命周期.
    """
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] monitor starting")
    start_time = _time.perf_counter()

    from quant.execution.calendar import is_trading_day

    today = partition_date
    if not is_trading_day():
        context.log.info(f"[{today}] non-trading day, monitor skipped")
        _add_metadata(context, partition_date, "skipped_non_trading", duration_ms=0)
        return {"date": today, "status": "skipped_non_trading"}

    now = datetime.now()
    hhmm = _dt_time(now.hour, now.minute)

    if not (_dt_time(9, 35) <= hhmm <= _dt_time(15, 0)):
        context.log.info(f"[{today}] outside monitor window (09:35-15:00), current={hhmm}")
        _add_metadata(context, partition_date, "skipped_outside_window", duration_ms=0)
        return {"date": today, "status": "skipped_outside_window"}

    # 在独立线程启动守护进程
    _daemon_stop = threading.Event()

    def _daemon_wrapper():
        try:
            from quant.scheduler.monitor import _run_continuous
            _run_continuous(today, stop_event=_daemon_stop)
        except Exception as e:
            context.log.error(f"[{today}] monitor daemon thread crashed: {e}")

    daemon_thread = threading.Thread(
        target=_daemon_wrapper,
        daemon=True,
        name=f"monitor-daemon-{today}",
    )
    daemon_thread.start()
    context.log.info(f"[{today}] monitor daemon thread started (thread={daemon_thread.name})")

    # 主线程立即返回, Asset 标记完成
    # 守护进程在后台持续运行直到收到 stop 信号
    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "daemon_started",
                 thread=daemon_thread.name,
                 duration_ms=duration_ms)
    return {"date": today, "status": "daemon_started",
            "thread": daemon_thread.name, "duration_ms": duration_ms}


@asset(
    description="尾盘快照 — 快照所有A股收盘价+全日量",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    ins={"monitor": AssetIn("monitor")},
    metadata={"owner": "data-engineering"},
)
def snapshot_close(
    context: AssetExecutionContext,
    monitor: dict,
    market_db: MarketDBResource,
) -> dict:
    """每日 15:00 运行."""
    # v594: snapshot_close 应使用当天日期, 而非 partition_date (前一天)
    from datetime import date as _date
    today = _date.today().isoformat()
    partition_date = context.partition_key
    context.log.info(f"[{today}] snapshot_close starting (partition={partition_date})")
    start_time = _time.perf_counter()

    from quant.scheduler.snapshot import snapshot_close as _snapshot_close
    result = _snapshot_close(today)

    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed",
                 saved=result.get("saved", 0),
                 errors=result.get("errors", 0),
                 duration_ms=duration_ms)
    return {"date": partition_date,
            "saved": result.get("saved", 0),
            "duration_ms": duration_ms}


@asset(
    description="日终对账 — OMS 对账闭环: 持仓/现金/订单三账核对",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    ins={"snapshot_close": AssetIn("snapshot_close")},
    metadata={"owner": "ops", "priority": "high"},
)
def reconcile(
    context: AssetExecutionContext,
    snapshot_close: dict,
    trade_repo: TradeRepoResource,
) -> dict:
    """每日 15:05 运行."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] reconcile starting")
    start_time = _time.perf_counter()

    from quant.scheduler.reconcile import _run as _reconcile_run
    result = _reconcile_run(partition_date)

    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed",
                 recon_status=result.get("recon_status", "ok"),
                 breaks=result.get("breaks", 0),
                 duration_ms=duration_ms)
    return {"date": partition_date,
            "recon_status": result.get("recon_status", "ok"),
            "breaks": result.get("breaks", 0),
            "duration_ms": duration_ms}


@asset(
    description="晚间链主流程 — daily_data 行情同步 (主流程)",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    metadata={"owner": "data-engineering", "priority": "high"},
)
def daily_data(
    context: AssetExecutionContext,
    market_db: MarketDBResource,
) -> dict:
    """每日 19:00 运行 - 晚间链第一阶段."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] daily_data starting")
    start_time = _time.perf_counter()

    from quant.scheduler.daily_data import _run as _daily_run
    # v625 (HANDOFF v624 regression): daily_data._run() is V586-wrapped and
    # manages its OWN task_log. The asset previously called _tk_start/_tk_finish
    # too -> double start made _run() early-return (rid=None) -> no actual work
    # -> false ok/failed. Delegate task_log to the module; the asset only
    # enforces the downstream-blocking post-condition (v594/v615/v616).
    _daily_run(partition_date)

    # v594: 检查 daily_data 实际状态, 非 ok 时阻止下游资产运行
    from quant.scheduler.task_log import last_status
    actual_status = last_status("daily_data", partition_date)
    duration_ms = (_time.perf_counter() - start_time) * 1000

    if actual_status != "ok":
        context.log.error(f"[{partition_date}] daily_data {actual_status} - blocking downstream assets")
        raise RuntimeError(f"daily_data {actual_status}")

    _add_metadata(context, partition_date, "completed", duration_ms=duration_ms)
    return {"date": partition_date, "status": "completed", "duration_ms": duration_ms}


@asset(
    description="复权因子同步 — 批量拉取 adj_factor 落本地表",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    ins={"daily_data": AssetIn("daily_data")},
    metadata={"owner": "data-engineering"},
)
def adj_factor(
    context: AssetExecutionContext,
    daily_data: dict,
    data_source_registry: DataSourceRegistryResource,
) -> dict:
    """晚间链第二阶段: 复权因子同步."""
    from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
    partition_date = context.partition_key
    rid = _tk_start("adj_factor", partition_date, grace_seconds=3600)
    if rid is None:
        context.log.info(f"[{partition_date}] adj_factor already running, skip")
        return {"date": partition_date, "status": "skipped", "duration_ms": 0}
    
    try:
        context.log.info(f"[{partition_date}] adj_factor starting")
        start_time = _time.perf_counter()

        from quant.data.store import DataStore
        store = DataStore()
        result = store.sync_adj_factor(max_batches=1)
        store.close()

        duration_ms = (_time.perf_counter() - start_time) * 1000
        _add_metadata(context, partition_date, "completed",
                     rows=result.get("rows", 0),
                     duration_ms=duration_ms)
        _tk_finish("adj_factor", partition_date, "ok")
        return {"date": partition_date,
                "rows": result.get("rows", 0),
                "duration_ms": duration_ms}
    except Exception as e:
        _tk_finish("adj_factor", partition_date, "failed", error=str(e))
        raise


@asset(
    description="DuckDB 增量同步 — SQLite→DuckDB (daily_data 后、factor_cache 前)",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    ins={"adj_factor": AssetIn("adj_factor")},
    metadata={"owner": "data-engineering", "priority": "high"},
)
def duckdb_sync(
    context: AssetExecutionContext,
    adj_factor: dict,
) -> dict:
    """晚间链第三阶段: DuckDB 增量同步."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] duckdb_sync starting")
    start_time = _time.perf_counter()

    from quant.scheduler.duckdb_sync import _run as _duckdb_run
    # v625: delegate task_log to duckdb_sync._run() (V586 self-managed)
    _duckdb_run(partition_date)

    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed", duration_ms=duration_ms)
    return {"date": partition_date, "status": "completed", "duration_ms": duration_ms}


@asset(
    description="因子物化 — 增量物化因子缓存到 gzip CSV (Legacy: 单进程, Dagster: 分布式可选)",
    partitions_def=trading_day_partitions,
    kinds={"python", "database", "compute"},
    ins={"duckdb_sync": AssetIn("duckdb_sync")},
    metadata={"owner": "quant-research", "priority": "high"},
)
def factor_cache(
    context: AssetExecutionContext,
    duckdb_sync: dict,
) -> dict:
    """晚间链第四阶段: 因子缓存物化.

    v627: Scoped date range fix — instead of full-range 2020-01-01→today (1622 dates,
    OOM-kill on 8GB M1), scope to last materialized date → today.
    trading_days.json records successfully materialized dates; resume from there.
    """
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] factor_cache starting")
    start_time = _time.perf_counter()

    from quant.config.constants import _require_cfg
    from quant.factor.store import FactorStore as _FS

    # v627: Scoped date range — same logic as repair.py _ensure_factor_cache
    _fc_start = _require_cfg("backtest.factor_cache_start")
    _fs = _FS()
    _td = _fs._load_trading_days()
    if _td:
        _last_ok = max(d for d in _td if d <= partition_date)
        _fc_start = _last_ok  # re-materialize last date (idempotent, 已物化跳过)
        context.log.info(f"[{partition_date}] factor_cache scoped to last_ok={_last_ok} → {partition_date} (OOM-safe)")

    # v627: Ray distributed engine integration
    _ray_enabled = _require_cfg("factor.distributed.enabled") if "factor.distributed.enabled" in _require_cfg.__code__.co_consts else False
    try:
        _ray_enabled = bool(_require_cfg("factor.distributed.enabled"))
    except Exception:
        _ray_enabled = False

    if _ray_enabled:
        # Use Ray distributed engine for parallel factor materialization
        context.log.info(f"[{partition_date}] factor_cache: using Ray distributed engine")
        from quant.factor.distributed.engine import run_distributed_factorization
        try:
            result = run_distributed_factorization(
                start_date=_fc_start,
                end_date=partition_date,
                partition_strategy="date",
                ray_config={"mode": "local"},
            )
            context.log.info(f"[{partition_date}] factor_cache: Ray distributed completed — {result}")
        except Exception as _ray_err:
            context.log.warning(f"[{partition_date}] factor_cache: Ray failed ({_ray_err}), falling back to single-process")
            from quant.scheduler.factor_cache import _run as _fc_run
            _fc_run(_fc_start, partition_date)
    else:
        # Fallback: single-process factor materialization
        from quant.scheduler.factor_cache import _run as _fc_run
        # v625: delegate task_log to factor_cache._run() (V586 self-managed)
        _fc_run(_fc_start, partition_date)

    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed", duration_ms=duration_ms)
    return {"date": partition_date, "status": "completed", "duration_ms": duration_ms}


@asset(
    description="归因分析 — Brinson/OOS/因子PnL/换手率/信号衰减/拥挤度/DSR",
    partitions_def=trading_day_partitions,
    kinds={"python", "analytics"},
    ins={"factor_cache": AssetIn("factor_cache")},
    metadata={"owner": "quant-research", "priority": "high"},
)
def attribution(
    context: AssetExecutionContext,
    factor_cache: dict,
    trade_repo: TradeRepoResource,
) -> dict:
    """晚间链第五阶段: 归因分析."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] attribution starting")
    start_time = _time.perf_counter()

    from quant.scheduler.attribution import _run as _attr_run
    # v625: delegate task_log to attribution._run() (@task self-managed)
    _attr_run(partition_date)

    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed", duration_ms=duration_ms)
    return {"date": partition_date, "status": "completed", "duration_ms": duration_ms}


@asset(
    description="LightGBM 模型训练 — 仅周一/周四",
    partitions_def=trading_day_partitions,
    kinds={"python", "ml"},
    ins={"attribution": AssetIn("attribution")},
    metadata={"owner": "ml-engineering"},
)
def lgb_train(
    context: AssetExecutionContext,
    attribution: dict,
) -> dict:
    """晚间链第六阶段: LGB 训练 (仅周一/四)."""
    partition_date = context.partition_key
    import pandas as pd
    wd = pd.Timestamp(partition_date).weekday()
    if wd not in (0, 3):
        context.log.info(f"[{partition_date}] lgb_train skipped (not Mon/Thu, wd={wd})")
        _add_metadata(context, partition_date, "skipped", reason=f"weekday={wd}")
        return {"date": partition_date, "status": "skipped", "weekday": wd}

    context.log.info(f"[{partition_date}] lgb_train starting")
    start_time = _time.perf_counter()

    from quant.scheduler.lgb_train import _run as _lgb_run
    # v625: delegate task_log to lgb_train._run() (V586 self-managed)
    _lgb_run(partition_date)

    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed", duration_ms=duration_ms)
    return {"date": partition_date, "status": "completed", "duration_ms": duration_ms}


@asset(
    description="XGBoost 模型训练 — 仅周一/周四",
    partitions_def=trading_day_partitions,
    kinds={"python", "ml"},
    ins={"attribution": AssetIn("attribution")},
    metadata={"owner": "ml-engineering"},
)
def xgb_train(
    context: AssetExecutionContext,
    attribution: dict,
) -> dict:
    """晚间链第七阶段: XGB 训练 (仅周一/四).

    v577 fix: 原函数体缺少实际调用 _run() (遗漏), 现补全.
    """
    partition_date = context.partition_key
    import pandas as pd
    wd = pd.Timestamp(partition_date).weekday()
    if wd not in (0, 3):
        context.log.info(f"[{partition_date}] xgb_train skipped (not Mon/Thu, wd={wd})")
        _add_metadata(context, partition_date, "skipped", reason=f"weekday={wd}")
        return {"date": partition_date, "status": "skipped", "weekday": wd}

    context.log.info(f"[{partition_date}] xgb_train starting")
    start_time = _time.perf_counter()

    from quant.scheduler.xgb_train import _run as _xgb_run
    # v625: delegate task_log to xgb_train._run() (V586 self-managed)
    _xgb_run(partition_date)

    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed", duration_ms=duration_ms)
    return {"date": partition_date, "status": "completed", "duration_ms": duration_ms}


# ═══════════════════════════════════════════════════════════════════
# 周度评估资产 (周六)
# ═══════════════════════════════════════════════════════════════════


@asset(
    description="周度因子评估全流程 — 策展→数据→IC→CPCV→成本→状态同步",
    partitions_def=weekly_partitions,
    kinds={"python", "analytics", "ml"},
    metadata={"owner": "quant-research", "priority": "high"},
)
def weekly_eval(
    context: AssetExecutionContext,
    data_source_registry: DataSourceRegistryResource,
    factor_store: FactorStoreResource,
) -> dict:
    """每周六 06:00 运行."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] weekly_eval starting")
    start_time = _time.perf_counter()

    from quant.scheduler.weekly import _run as _weekly_run
    from quant.scheduler.task_log import finish as _tk_finish
    # v625: weekly._run() lacks outer try/finally (soft-fail 5/7 PBO gate); this
    # guard + the SubprocessRunner net guarantee the row finishes on crash.
    try:
        _weekly_run(partition_date)
    except Exception as e:
        context.log.exception(f"[{partition_date}] weekly_eval task failed")
        try:
            _tk_finish("weekly_eval", partition_date, "failed", error=str(e))
        except Exception as _fe:
            context.log.warning(f"weekly_eval finish-on-crash failed: {_fe}")
        raise

    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed", duration_ms=duration_ms)
    return {"date": partition_date, "status": "completed", "duration_ms": duration_ms}


# ═══════════════════════════════════════════════════════════════════
# 作业定义
# ═══════════════════════════════════════════════════════════════════

# AM 链: 早间补拉 → 信号 → 执行 → 开盘快照 → 盘中风控
# v569 fix: AM 链不含晚间任务, 避免与 daily_data_job 重复
daily_trading_job = define_asset_job(
    name="daily_trading_job",
    selection=AssetSelection.keys(
        "daily_repair",
        # v594: signals/execute/snapshot_open 从 daily_trading_job 中移除, 改为独立 schedule
        # "signals",
        # "execute",
        # "snapshot_open",
        "monitor",
    ),
    partitions_def=trading_day_partitions,
    description="AM链: 早间补拉 → 盘中风控",
    op_retry_policy=RETRY_POLICY,
)

# v594: execute 独立 schedule - 09:20 触发
execute_job = define_asset_job(
    name="execute_job",
    selection=AssetSelection.keys("execute"),
    partitions_def=trading_day_partitions,
    description="交易执行 - 09:20",
)

# v594: signals 独立 schedule - 08:30 触发
signals_job = define_asset_job(
    name="signals_job",
    selection=AssetSelection.keys("signals"),
    partitions_def=trading_day_partitions,
    description="信号生成 - 08:30",
)

# v594: snapshot_open 独立 schedule - 10:00 触发
snapshot_open_job = define_asset_job(
    name="snapshot_open_job",
    selection=AssetSelection.keys("snapshot_open"),
    partitions_def=trading_day_partitions,
    description="开盘快照 - 10:00",
)


# PM 链: 尾盘快照 → 日终对账
end_of_day_job = define_asset_job(
    name="end_of_day_job",
    selection=AssetSelection.keys("snapshot_close", "reconcile"),
    partitions_def=trading_day_partitions,
    description="PM链: 尾盘快照 → 日终对账",
    op_retry_policy=RETRY_POLICY,
)

# 晚间链: daily_data → adj_factor → duckdb_sync → factor_cache → attribution → ML训练
daily_data_job = define_asset_job(
    name="daily_data_job",
    selection=AssetSelection.keys(
        "daily_data",
        "adj_factor",
        "duckdb_sync",
        "factor_cache",
        "attribution",
        "lgb_train",
        "xgb_train",
    ),
    partitions_def=trading_day_partitions,
    description="晚间链: daily_data → adj_factor → DuckDB → 因子缓存 → 归因 → ML训练",
    op_retry_policy=RETRY_POLICY,
)

# 周度评估
weekly_evaluation_job = define_asset_job(
    name="weekly_evaluation_job",
    selection=AssetSelection.keys("weekly_eval"),
    partitions_def=weekly_partitions,
    description="周六因子评估全流程",
    op_retry_policy=RetryPolicy(
        max_retries=2,
        delay=60,
        backoff=Backoff.EXPONENTIAL,
        jitter=Jitter.PLUS_MINUS,
    ),
)


# ═══════════════════════════════════════════════════════════════════
# 调度定义
# ═══════════════════════════════════════════════════════════════════

# ── schedule helper: 传递 partition key ──────────────────────────────────────

# v577 fix: Dagster 默认 _execution_fn 生成的 RunRequest 不带 partition_key，
# 导致 define_asset_job(..., partitions_def=...) 的 partitioned job
# 以非分区模式运行，资产访问 context.partition_key 时崩溃:
#   'Cannot access partition_key for a non-partitioned run'
#
# 解决方案: 为每个 partitioned job 显式写 execution_fn，传递 partition_key


def _make_partitioned_schedule(
    schedule_name: str,
    job: dg.UnresolvedAssetJobDefinition,
    cron_schedule: str,
    execution_timezone: str,
    get_partition_key: callable,
) -> dg.ScheduleDefinition:
    """为 partitioned job 创建 schedule，自动传递 partition_key 到 RunRequest.

    v577 fix: Dagster 默认 _execution_fn 生成的 RunRequest 不带 partition_key，
    导致 define_asset_job(..., partitions_def=...) 的 partitioned job
    以非分区模式运行，资产访问 context.partition_key 时崩溃:
      'Cannot access partition_key for a non-partitioned run'
    """

    def _execution_fn(
        context: dg.ScheduleEvaluationContext,
    ) -> dg.RunRequest:
        partition_key = get_partition_key(context.scheduled_execution_time)
        return dg.RunRequest(
            partition_key=partition_key,
            run_key=partition_key,  # run_key 用于幂等去重
            run_config={},
        )

    return dg.ScheduleDefinition(
        name=schedule_name,
        job=job,
        cron_schedule=cron_schedule,
        execution_timezone=execution_timezone,
        default_status=DefaultScheduleStatus.RUNNING,
        execution_fn=_execution_fn,
    )


# v594: execute 独立 schedule - 09:20 触发
execute_job = define_asset_job(
    name="execute_job",
    selection=AssetSelection.keys("execute"),
    partitions_def=trading_day_partitions,
    description="交易执行 - 09:20",
)


signals_job = define_asset_job(
    name="signals_job",
    selection=AssetSelection.keys("signals"),
    partitions_def=trading_day_partitions,
    description="信号生成 - 08:30",
)

def _am_partition_key(scheduled_time: datetime.datetime) -> str:
    # 使用 partitions_def 的官方计算方法，确保 partition key 格式正确
    return trading_day_partitions.get_partition_key_for_timestamp(
        (scheduled_time - timedelta(days=1)).timestamp(), None
    )


# v594: AM 链独立 schedules
# signals 独立 schedule - 08:30 触发
signals_schedule = _make_partitioned_schedule(
    schedule_name="signals_schedule",
    job=signals_job,
    cron_schedule="30 8 * * 1-5",
    execution_timezone="Asia/Shanghai",
    get_partition_key=_am_partition_key,
)

# execute 独立 schedule - 09:20 触发
execute_schedule = _make_partitioned_schedule(
    schedule_name="execute_schedule",
    job=execute_job,
    cron_schedule="20 9 * * 1-5",
    execution_timezone="Asia/Shanghai",
    get_partition_key=_am_partition_key,
)

# snapshot_open 独立 schedule - 10:00 触发
snapshot_open_schedule = _make_partitioned_schedule(
    schedule_name="snapshot_open_schedule",
    job=snapshot_open_job,
    cron_schedule="0 10 * * 1-5",
    execution_timezone="Asia/Shanghai",
    get_partition_key=_am_partition_key,
)

daily_trading_schedule = _make_partitioned_schedule(
    schedule_name="daily_trading_job_schedule",
    job=daily_trading_job,
    cron_schedule="0 5 * * 1-5",
    execution_timezone="Asia/Shanghai",
    get_partition_key=_am_partition_key,
)


# ── PM 链: 交易日 15:00 触发 ─────────────────────────────────────────────────
# 尾盘快照在交易日 15:00 执行，partition = 当天


def _pm_partition_key(scheduled_time: datetime.datetime) -> str:
    return trading_day_partitions.get_partition_key_for_timestamp(
        scheduled_time.timestamp(), None
    )


end_of_day_schedule = _make_partitioned_schedule(
    schedule_name="end_of_day_job_schedule",
    job=end_of_day_job,
    cron_schedule="5 15 * * 1-5",  # v614 fix: 从 15:00 改为 15:05，与 reconcile 窗口一致
    execution_timezone="Asia/Shanghai",
    get_partition_key=_pm_partition_key,
)

# ── 晚间链: 交易日 19:00 触发 ─────────────────────────────────────────────────
# 补数据到当天 (前一日收盘后已有数据，但补拉在 19:00 触发)


def _evening_partition_key(scheduled_time: datetime.datetime) -> str:
    return trading_day_partitions.get_partition_key_for_timestamp(
        scheduled_time.timestamp(), None
    )


daily_data_schedule = _make_partitioned_schedule(
    schedule_name="daily_data_job_schedule",
    job=daily_data_job,
    cron_schedule="0 19 * * 1-5",
    execution_timezone="Asia/Shanghai",
    get_partition_key=_evening_partition_key,
)

# ── 周度评估: 周六 06:00 触发 ─────────────────────────────────────────────────
# WeeklyPartitionsDefinition: day_of_week=5 (周六启动), partition key = 周日起始
# 例如 2026-08-31 (周一) 触发时, partition key 应为 2026-08-30 (周日)


def _weekly_partition_key(scheduled_time: datetime.datetime) -> str:
    # 使用 weekly_partitions 的官方计算方法，确保 partition key 与定义一致
    return weekly_partitions.get_partition_key_for_timestamp(
        scheduled_time.timestamp(), None
    )


weekly_evaluation_schedule = _make_partitioned_schedule(
    schedule_name="weekly_evaluation_job_schedule",
    job=weekly_evaluation_job,
    cron_schedule="0 6 * * 6",
    execution_timezone="Asia/Shanghai",
    get_partition_key=_weekly_partition_key,
)


# ═══════════════════════════════════════════════════════════════════
# Sensor: monitor 守护进程生命周期管理
# ═══════════════════════════════════════════════════════════════════

monitor_sensor_job = define_asset_job(
    name="monitor_sensor_job",
    selection=AssetSelection.keys("monitor"),
    partitions_def=trading_day_partitions,
)


@dg.sensor(
    job=monitor_sensor_job,
    default_status=DefaultSensorStatus.RUNNING,
    minimum_interval_seconds=30,
)
def monitor_sensor(context: dg.SensorEvaluationContext):
    """盘中风控 Sensor — 控制 monitor daemon 守护进程的生命周期.

    逻辑:
      - 非交易日: Skip
      - 盘前 (09:25-09:35): 触发 monitor Asset (启动守护线程)
      - 午休 (11:30-13:00): Skip (monitor 内部处理午休暂停)
      - 收盘后 (>=15:00): Skip (monitor 守护线程自退)
      - 交易时段内定期健康检查: 每 30s 发一次 RunRequest 保持分区活跃
    """
    from quant.execution.calendar import is_trading_day, get_trading_period
    now = datetime.now()
    today = now.date()

    if not is_trading_day(today):
        return SkipReason(f"{today} 非交易日")

    period = get_trading_period(now)
    hhmm = now.time()

    if period == "盘前":
        if hhmm >= _dt_time(9, 25):
            return RunRequest(
                partition_key=today.isoformat(),
                tags={"trigger": "market_open", "period": period},
            )
        return SkipReason(f"market not yet open (period={period})")
    elif period in ("上午交易", "下午交易"):
        # 交易时段健康检查
        return RunRequest(
            partition_key=today.isoformat(),
            tags={"trigger": "health_check", "period": period},
        )
    elif period == "午休":
        return SkipReason(f"lunch break, period={period}")
    else:
        return SkipReason(f"market closed, period={period}")


# ═══════════════════════════════════════════════════════════════════
# Definitions
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
# v627: 回测并行执行 — Dagster Job + 分区并行
# ═══════════════════════════════════════════════════════════════════

backtest_partitions = DailyPartitionsDefinition(
    start_date="2020-01-01",
    end_offset=1,
    timezone="Asia/Shanghai",
)


@asset(
    description="回测执行 — 单个分区回测 (可并行运行多个)",
    partitions_def=backtest_partitions,
    kinds={"python", "backtest"},
    metadata={"owner": "quant-research", "priority": "medium"},
)
def backtest_asset(
    context: AssetExecutionContext,
    factor_store: FactorStoreResource,
) -> dict:
    """单个分区回测 — 可通过 Dagster Job 并行运行多个分区.

    v627: 回测并行执行 — 每个分区独立运行, 无数据依赖.
    """
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] backtest starting")
    start_time = _time.perf_counter()

    # 计算回测日期范围 (分区日期 = 回测结束日期, 起始日期 = 前一年)
    import pandas as pd
    end_date = partition_date
    start_date = (pd.Timestamp(partition_date) - pd.DateOffset(years=1)).strftime("%Y-%m-%d")

    from quant.backtest.loop import run_backtest
    try:
        result = run_backtest(
            start_date=start_date,
            end_date=end_date,
            capital=5000,
            mode="smoke",
            universe_size=100,
        )
        metrics = result.get("metrics", {})
        sharpe = metrics.get("sharpe_ratio", 0)
        total_return = metrics.get("total_return", 0)
        context.log.info(f"[{partition_date}] backtest done: sharpe={sharpe:.3f}, return={total_return:.2%}")
    except Exception as e:
        context.log.exception(f"[{partition_date}] backtest failed: {e}")
        sharpe = 0
        total_return = 0

    duration_ms = (_time.perf_counter() - start_time) * 1000
    _add_metadata(context, partition_date, "completed",
                 sharpe=sharpe, total_return=total_return, duration_ms=duration_ms)
    return {"date": partition_date, "sharpe": sharpe, "total_return": total_return,
            "duration_ms": duration_ms}


# 回测 Job — 可并行运行多个分区
backtest_job = define_asset_job(
    name="backtest_job",
    selection=AssetSelection.keys("backtest_asset"),
    partitions_def=backtest_partitions,
    description="回测执行 - 可并行运行多个分区",
    op_retry_policy=RetryPolicy(
        max_retries=2,
        delay=30,
        backoff=Backoff.EXPONENTIAL,
        jitter=Jitter.PLUS_MINUS,
    ),
)


def _backtest_partition_key(scheduled_time: datetime.datetime) -> str:
    return backtest_partitions.get_partition_key_for_timestamp(
        scheduled_time.timestamp(), None
    )


backtest_schedule = _make_partitioned_schedule(
    schedule_name="backtest_schedule",
    job=backtest_job,
    cron_schedule="0 22 * * 1-5",  # 每个交易日 22:00 运行回测
    execution_timezone="Asia/Shanghai",
    get_partition_key=_backtest_partition_key,
)


def get_definitions():
    return dg.Definitions(
        assets=[
            daily_repair,
            signals,
            execute,
            snapshot_open,
            monitor,
            snapshot_close,
            reconcile,
            daily_data,
            adj_factor,
            duckdb_sync,
            factor_cache,
            attribution,
            lgb_train,
            xgb_train,
            weekly_eval,
            backtest_asset,
        ],
        jobs=[daily_trading_job, end_of_day_job, daily_data_job, weekly_evaluation_job, backtest_job],
        schedules=[
            daily_trading_schedule,
            signals_schedule,
            execute_schedule,
            snapshot_open_schedule,
            end_of_day_schedule,
            daily_data_schedule,
            weekly_evaluation_schedule,
            backtest_schedule,
        ],
        sensors=[monitor_sensor],
        resources={
            "data_source_registry": DataSourceRegistryResource(),
            "factor_store": FactorStoreResource(),
            "trade_repo": TradeRepoResource(),
            "market_db": MarketDBResource(),
        },
    )


definitions = get_definitions()
