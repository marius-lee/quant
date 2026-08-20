"""Dagster 资产/作业定义 — 替代自研 orchestrator.

设计:
  - 每个调度任务 = 一个 Dagster Asset/Op
  - 依赖通过 AssetIn/AssetOut 显式声明
  - 时间分区: DailyPartitionsDefinition (交易日) + WeeklyPartitionsDefinition (周六)
  - 资源: DataSourceRegistry, FactorStore, TradeRepo 等
  - 调度器: Dagster Daemon (cron) 替代自研 30s 轮询
  - 可观测: Dagster UI + 结构化日志 + 指标导出
"""

import time
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
    Config,
    RetryPolicy,
)
from datetime import datetime, date, time
from typing import Optional

# ═══════════════════════════════════════════════════════════════════
# 分区定义
# ═══════════════════════════════════════════════════════════════════

trading_day_partitions = DailyPartitionsDefinition(
    start_date="2020-01-01",
    end_offset=0,  # 运行到今天
    timezone="Asia/Shanghai",
)

weekly_partitions = WeeklyPartitionsDefinition(
    start_date="2024-01-06",  # 第一个周六
    end_offset=0,
    day_of_week=5,  # 周六
    timezone="Asia/Shanghai",
)

# ══════════════════════════════════════════════════════════════════
# 资源定义 — 支持多环境 (Dev/Staging/Prod) + EnvVar 注入
# ══════════════════════════════════════════════════════════════════

from dagster import EnvVar

class DataSourceRegistryResource(dg.ConfigurableResource):
    """数据源注册表资源."""
    state_dir: str = "/tmp/quant_sources"
    # 环境变量注入: QUANT_STATE_DIR
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
    """因子存储资源."""
    db_path: str = "quant/data/factor_cache.db"
    db_path_env: Optional[str] = None

    def __post_init__(self):
        if self.db_path_env:
            self.db_path = EnvVar(self.db_path_env).get_value()

    def get_client(self):
        from quant.factor.store import FactorStore
        return FactorStore(db_path=self.db_path)


class TradeRepoResource(dg.ConfigurableResource):
    """交易仓库资源."""
    db_path: str = "quant/data/trades.db"
    db_path_env: Optional[str] = None

    def __post_init__(self):
        if self.db_path_env:
            self.db_path = EnvVar(self.db_path_env).get_value()

    def get_client(self):
        from quant.data.repos import TradeRepo
        return TradeRepo(db_path=self.db_path)


class MarketDBResource(dg.ConfigurableResource):
    """行情数据库资源."""
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
# 资产定义 — 对应 manifest.py 中的每个任务
# ═══════════════════════════════════════════════════════════════════

@asset(
    description="早间补拉链 — 重试前3天审计失败表 + 7天未OK的weekly_full表 + factor_cache兜底",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    metadata={"owner": "data-engineering", "priority": "high"},
)
def daily_repair(
    context: AssetExecutionContext,
    market_db: ResourceParam[MarketDBResource],
) -> dict:
    """每日 05:00 运行 (非交易日也运行, 覆盖周五晚间链缺口)."""
    partition_date = context.partition_key  # YYYY-MM-DD
    context.log.info(f"[{partition_date}] daily_repair starting")

    start_time = time.perf_counter()
    
    from quant.scheduler.repair import _run
    _run(partition_date)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    # 添加输出元数据
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "status": "completed",
    })
    
    return {"date": partition_date, "status": "completed"}

@asset(
    description="分布式因子物化 — 基于 Ray 并行计算 (替代单进程 factor_cache)",
    partitions_def=trading_day_partitions,
    kinds={"python", "database", "compute", "ray"},
    ins={"adj_factor": AssetIn("adj_factor")},
    metadata={"owner": "quant-research", "priority": "high"},
)
def factor_cache_distributed(
    context: AssetExecutionContext,
    adj_factor: dict,
) -> dict:
    """晚间链第三阶段: 分布式因子缓存物化."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] factor_cache_distributed starting")

    from quant.config.constants import _require_cfg
    from quant.factor.distributed import run_distributed_factorization

    # 检查是否启用分布式模式
    from quant.config.loader import load as _load_config
    cfg = _load_config()
    distributed_enabled = cfg.get('factor', {}).get('distributed', {}).get('enabled', False)

    if not distributed_enabled:
        context.log.info("Distributed factorization disabled, falling back to single-process")
        from quant.scheduler.factor_cache import _run as _fc_run
        _fc_start = _require_cfg("backtest.factor_cache_start")
        _fc_run(_fc_start, partition_date)
        return {"date": partition_date, "status": "completed (single-process fallback)"}

    # 分布式执行
    ray_config = cfg.get('factor', {}).get('distributed', {}).get('ray', {})
    partition_strategy = cfg.get('factor', {}).get('distributed', {}).get('partition_strategy', 'date')
    partition_kwargs = cfg.get('factor', {}).get('distributed', {}).get('partition_kwargs', {})

    start_time = time.perf_counter()
    result = run_distributed_factorization(
        start_date=_require_cfg('backtest.factor_cache_start'),
        end_date=partition_date,
        partition_strategy=partition_strategy,
        partition_kwargs=partition_kwargs,
        ray_config=ray_config,
    )
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "status": "completed",
        "summary": str(result),
    })

    context.log.info(f"[{partition_date}] factor_cache_distributed completed: {result}")
    return {"date": partition_date, "status": "completed", "summary": result}




@asset(
    description="信号生成 — 计算所有using因子, 生成Alpha信号与目标持仓",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    ins={"daily_repair": AssetIn("daily_repair")},  # 依赖 early repair 尝试过即可
    metadata={"owner": "quant-research", "priority": "high"},
)
def signals(
    context: AssetExecutionContext,
    daily_repair: dict,
    factor_store: ResourceParam[FactorStoreResource],
) -> dict:
    """每日 08:30 运行."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] signals starting")

    start_time = time.perf_counter()
    
    from quant.scheduler.signals import _run
    result = _run(partition_date)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "targets": result["targets"],
        "elapsed": result["elapsed"],
        "status": "completed",
    })

    return {"date": partition_date, "targets": result["targets"], "elapsed": result["elapsed"]}


@asset(
    description="交易执行 — 读取信号、获取行情、执行调仓订单",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    ins={"signals": AssetIn("signals")},  # 依赖 signals 尝试过即可
    metadata={"owner": "execution", "priority": "high"},
)
def execute(
    context: AssetExecutionContext,
    signals: dict,
    trade_repo: ResourceParam[TradeRepoResource],
) -> dict:
    """每日 09:30 运行 (仅调仓日)."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] execute starting")

    start_time = time.perf_counter()
    
    from quant.scheduler.execute import _run
    result = _run(partition_date)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "sells": result["sells"],
        "limit_buys": result["limit_buys"],
        "status": "completed",
    })

    return {"date": partition_date, "sells": result["sells"], "limit_buys": result["limit_buys"]}


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
    market_db: ResourceParam[MarketDBResource],
) -> dict:
    """每日 10:00 运行."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] snapshot_open starting")

    start_time = time.perf_counter()
    
    from quant.scheduler.snapshot import snapshot_open as _snapshot_open
    result = _snapshot_open(partition_date)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "saved": result["saved"],
        "errors": result["errors"],
        "status": "completed",
    })

    return {"date": partition_date, "saved": result["saved"]}


@asset(
    description="盘中风控 — 每30秒轮询 止损/止盈/熔断, 触发后立即卖出",
    partitions_def=trading_day_partitions,
    kinds={"python", "monitoring"},
    metadata={"owner": "risk", "priority": "critical"},
)
def monitor(
    context: AssetExecutionContext,
    market_db: ResourceParam[MarketDBResource],
) -> dict:
    """09:35-15:00 持续运行 (午休内部暂停). 作为 Sensor 触发的长驻作业."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] monitor daemon starting")

    # 注意: 盘中风控适合用 Sensor + 长驻进程, 这里简化为启动标记
    # 实际运行由 monitor_sensor 触发的独立进程管理
    from quant.scheduler.monitor import _run_continuous
    # 不直接调用 _run_continuous (会阻塞), 而是记录启动意图
    # 真实部署时: 由 K8s CronJob 或 systemd 管理 monitor 守护进程

    context.add_output_metadata({
        "partition_date": partition_date,
        "status": "daemon_started",
    })

    return {"date": partition_date, "status": "daemon_started"}


@asset(
    description="尾盘快照 — 快照所有A股收盘价+全日量",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    metadata={"owner": "data-engineering"},
)
def snapshot_close(
    context: AssetExecutionContext,
    market_db: ResourceParam[MarketDBResource],
) -> dict:
    """每日 15:00 运行."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] snapshot_close starting")

    start_time = time.perf_counter()
    
    from quant.scheduler.snapshot import snapshot_close as _snapshot_close
    result = _snapshot_close(partition_date)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "saved": result["saved"],
        "errors": result["errors"],
        "status": "completed",
    })

    return {"date": partition_date, "saved": result["saved"]}


@asset(
    description="日终对账 — OMS 对账闭环: 持仓/现金/订单三账核对",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    ins={"monitor": AssetIn("monitor")},  # 依赖 monitor 尝试过即可
    metadata={"owner": "ops", "priority": "high"},
)
def reconcile(
    context: AssetExecutionContext,
    monitor: dict,
    trade_repo: ResourceParam[TradeRepoResource],
) -> dict:
    """每日 15:05 运行."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] reconcile starting")

    start_time = time.perf_counter()
    
    from quant.scheduler.reconcile import _run
    result = _run(partition_date)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "recon_status": result["recon_status"],
        "status": "completed",
    })

    return {"date": partition_date, "recon_status": result["recon_status"]}


@asset(
    description="晚间链主流程 — daily_data 行情同步 (主流程)",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    metadata={"owner": "data-engineering", "priority": "high"},
)
def daily_data(
    context: AssetExecutionContext,
    market_db: ResourceParam[MarketDBResource],
) -> dict:
    """每日 19:00 运行 - 晚间链第一阶段."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] daily_data starting")

    start_time = time.perf_counter()
    
    from quant.scheduler.daily_data import _run
    _run(partition_date)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "status": "completed",
    })

    return {"date": partition_date, "status": "completed"}


@asset(
    description="复权因子同步 — 批量拉取 adj_factor 落本地表",
    partitions_def=trading_day_partitions,
    kinds={"python", "database"},
    ins={"daily_data": AssetIn("daily_data")},  # 依赖 daily_data 成功
    metadata={"owner": "data-engineering"},
)
def adj_factor(
    context: AssetExecutionContext,
    daily_data: dict,
    data_source_registry: ResourceParam[DataSourceRegistryResource],
) -> dict:
    """晚间链第二阶段: 复权因子同步."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] adj_factor starting")

    start_time = time.perf_counter()
    
    from quant.data.store import DataStore
    store = DataStore()
    result = store.sync_adj_factor(max_batches=1)
    store.close()
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "rows": result.get("rows", 0),
        "status": "completed",
    })

    return {"date": partition_date, "rows": result.get("rows", 0)}


@asset(
    description="因子物化 — 增量物化因子缓存到 gzip CSV",
    partitions_def=trading_day_partitions,
    kinds={"python", "database", "compute"},
    ins={"adj_factor": AssetIn("adj_factor")},  # 依赖 adj_factor 成功
    metadata={"owner": "quant-research", "priority": "high"},
)
def factor_cache(
    context: AssetExecutionContext,
    adj_factor: dict,
    factor_store: ResourceParam[FactorStoreResource],
) -> dict:
    """晚间链第三阶段: 因子缓存物化."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] factor_cache starting")

    start_time = time.perf_counter()
    
    from quant.config.constants import _require_cfg
    from quant.scheduler.factor_cache import _run as _fc_run
    _fc_start = _require_cfg("backtest.factor_cache_start")
    _fc_run(_fc_start, partition_date)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "status": "completed",
    })

    return {"date": partition_date, "status": "completed"}


@asset(
    description="归因分析 — Brinson/OOS/因子PnL/换手率/信号衰减/拥挤度/DSR",
    partitions_def=trading_day_partitions,
    kinds={"python", "analytics"},
    ins={"factor_cache": AssetIn("factor_cache")},  # 依赖 factor_cache 成功
    metadata={"owner": "quant-research", "priority": "high"},
)
def attribution(
    context: AssetExecutionContext,
    factor_cache: dict,
    trade_repo: ResourceParam[TradeRepoResource],
) -> dict:
    """晚间链第四阶段: 归因分析."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] attribution starting")

    start_time = time.perf_counter()
    
    from quant.scheduler.attribution import _run
    _run(partition_date)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "status": "completed",
    })

    return {"date": partition_date, "status": "completed"}


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
    """晚间链第五阶段: LGB 训练 (仅周一/四)."""
    partition_date = context.partition_key
    import pandas as pd
    wd = pd.Timestamp(partition_date).weekday()
    if wd not in (0, 3):
        context.log.info(f"[{partition_date}] lgb_train skipped (not Mon/Thu, wd={wd})")
        return {"date": partition_date, "status": "skipped"}

    context.log.info(f"[{partition_date}] lgb_train starting")

    start_time = time.perf_counter()
    
    from quant.scheduler.lgb_train import _run
    _run(partition_date)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "status": "completed",
    })

    return {"date": partition_date, "status": "completed"}


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
    """晚间链第六阶段: XGB 训练 (仅周一/四)."""
    partition_date = context.partition_key
    import pandas as pd
    wd = pd.Timestamp(partition_date).weekday()
    if wd not in (0, 3):
        context.log.info(f"[{partition_date}] xgb_train skipped (not Mon/Thu, wd={wd})")
        return {"date": partition_date, "status": "skipped"}

    context.log.info(f"[{partition_date}] xgb_train starting")

    start_time = time.perf_counter()
    
    from quant.scheduler.xgb_train import _run
    _run(partition_date)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "status": "completed",
    })

    return {"date": partition_date, "status": "completed"}


# ═══════════════════════════════════════════════════════════════════
# 周度评估资产 (周六)
# ══════════════════════════════════════════════════════════════════

@asset(
    description="周度因子评估全流程 — 策展→数据→IC→CPCV→成本→状态同步",
    partitions_def=weekly_partitions,
    kinds={"python", "analytics", "ml"},
    metadata={"owner": "quant-research", "priority": "high"},
)
def weekly_eval(
    context: AssetExecutionContext,
    data_source_registry: ResourceParam[DataSourceRegistryResource],
    factor_store: ResourceParam[FactorStoreResource],
) -> dict:
    """每周六 06:00 运行."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] weekly_eval starting")

    start_time = time.perf_counter()
    
    from quant.scheduler.weekly import _run
    _run(partition_date)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    
    context.add_output_metadata({
        "duration_ms": duration_ms,
        "partition_date": partition_date,
        "status": "completed",
    })

    return {"date": partition_date, "status": "completed"}


# ══════════════════════════════════════════════════════════════════
# 作业定义
# ══════════════════════════════════════════════════════════════════

# 日线作业: daily_repair → signals → execute → snapshot_open → monitor → snapshot_close → reconcile → evening_chain
# evening_chain = daily_data → adj_factor → factor_cache → attribution → [lgb_train, xgb_train]

# 重试策略: 瞬时失败自动恢复，永久失败快速失败
RETRY_POLICY = RetryPolicy(
    max_retries=3,
    delay=10,  # 10秒基础延迟
    backoff=2.0,  # 指数退避
    jitter=0.1,
    # 仅重试特定错误类型
    retry_on_asset_failure=True,
)

daily_job = define_asset_job(
    name="daily_trading_job",
    selection=AssetSelection.keys(
        "daily_repair",
        "signals",
        "execute",
        "snapshot_open",
        "monitor",
        "snapshot_close",
        "reconcile",
        "daily_data",
        "adj_factor",
        "factor_cache",
        "factor_cache_distributed",
        "attribution",
        "lgb_train",
        "xgb_train",
    ),
    partitions_def=trading_day_partitions,
    description="交易日全流程: 早间补拉 → 信号 → 执行 → 快照 → 盘中风控 → 对账 → 晚间链",
    retry_policy=RETRY_POLICY,
)

weekly_job = define_asset_job(
    name="weekly_evaluation_job",
    selection=AssetSelection.keys("weekly_eval"),
    partitions_def=weekly_partitions,
    description="周六因子评估全流程",
    retry_policy=RetryPolicy(
        max_retries=2,  # 周度评估重试少一点
        delay=60,  # 1分钟基础延迟
        backoff=2.0,
        jitter=0.1,
        retry_on_asset_failure=True,
    ),
)

# ═══════════════════════════════════════════════════════════════════
# 调度定义 — 替代自研 orchestrator 的 30s 轮询
# ══════════════════════════════════════════════════════════════════

# 交易日作业调度: 由 Dagster Daemon 按分区自动触发
# 实际触发时间由资产的 `auto_materialize_policy` 或显式 Schedule 控制

daily_schedule = ScheduleDefinition(
    job=daily_job,
    cron_schedule="0 5 * * 1-5",  # 交易日 05:00 启动 (daily_repair), 后续任务按依赖自动触发
    execution_timezone="Asia/Shanghai",
    default_status=DefaultScheduleStatus.RUNNING,
)

weekly_schedule = ScheduleDefinition(
    job=weekly_job,
    cron_schedule="0 6 * * 6",  # 周六 06:00
    execution_timezone="Asia/Shanghai",
    default_status=DefaultScheduleStatus.RUNNING,
)

# ═══════════════════════════════════════════════════════════════════
# Sensor 定义 — 盘中风控守护进程管理
# ═════════════════════════════════════════════════════════════════

@dg.sensor(
    job=define_asset_job(
        name="monitor_daemon_job",
        selection=AssetSelection.keys("monitor"),
        partitions_def=trading_day_partitions,
    ),
    default_status=DefaultSensorStatus.RUNNING,
    minimum_interval_seconds=30,  # 30秒检查一次，更精确
)
def monitor_sensor(context: dg.SensorEvaluationContext):
    """盘中风控 Sensor — 基于实时行情时间窗精确控制 monitor 守护进程.

    逻辑 (使用 quant.execution.calendar 精确判断):
      - 非交易日: 不触发
      - 开盘前 (is_market_open == False, get_trading_period == "盘前"): 启动 monitor 守护进程
      - 交易时段 (is_market_open == True): 定期检查进程存活
      - 午休 (get_trading_period == "午休"): 暂停检查，等待下午开市
      - 收盘后 (get_trading_period == "盘后"): 停止 monitor 守护进程
      - 休市日: 不触发
    """
    from quant.execution.calendar import is_trading_day, is_market_open, get_trading_period
    now = datetime.now()
    today = now.date()

    if not is_trading_day(today):
        return SkipReason(f"{today} 非交易日")

    period = get_trading_period(now)
    
    if period == "盘前":
        # 开盘前 5 分钟内启动守护进程
        hhmm = now.time()
        if hhmm >= time(9, 25):
            yield RunRequest(
                partition_key=date.today().isoformat(),
                tags={"trigger": "market_open", "period": period},
            )
        else:
            return SkipReason(f"market not yet open, period={period}")
    elif period in ("上午交易", "下午交易"):
        # 交易时段: 定期检查守护进程存活
        yield RunRequest(
            partition_key=date.today().isoformat(),
            tags={"trigger": "health_check", "period": period},
        )
    elif period == "午休":
        # 午休期间暂停检查，等待下午开市
        return SkipReason(f"lunch break, period={period}")
    elif period in ("盘后", "休市"):
        # 收盘后: 停止触发，守护进程自退
        return SkipReason(f"market closed, period={period}")
    else:
        return SkipReason(f"unknown period: {period}")


# ══════════════════════════════════════════════════════════════════
# Definitions 导出
# ═════════════════════════════════════════════════════════════════

def get_definitions():
    """Dagster Definitions 入口."""
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
            factor_cache,
            factor_cache_distributed,
            attribution,
            lgb_train,
            xgb_train,
            weekly_eval,
        ],
        jobs=[daily_job, weekly_job],
        schedules=[daily_schedule, weekly_schedule],
        sensors=[monitor_sensor],
        resources={
            "data_source_registry": DataSourceRegistryResource(),
            "factor_store": FactorStoreResource(),
            "trade_repo": TradeRepoResource(),
            "market_db": MarketDBResource(),
        },
    )


# 导出供 `dagster dev` / `dagster-webserver` 使用
definitions = get_definitions()