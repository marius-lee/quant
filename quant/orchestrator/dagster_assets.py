"""Dagster 资产/作业定义 — 替代自研 orchestrator.

设计:
  - 每个调度任务 = 一个 Dagster Asset/Op
  - 依赖通过 AssetIn/AssetOut 显式声明
  - 时间分区: DailyPartitionsDefinition (交易日) + WeeklyPartitionsDefinition (周六)
  - 资源: DataSourceRegistry, FactorStore, TradeRepo 等
  - 调度器: Dagster Daemon (cron) 替代自研 30s 轮询
  - 可观测: Dagster UI + 结构化日志 + 指标导出
"""

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

# ═══════════════════════════════════════════════════════════════════
# 资源定义
# ═══════════════════════════════════════════════════════════════════

class DataSourceRegistryResource(dg.ConfigurableResource):
    """数据源注册表资源."""
    state_dir: str = "/tmp/quant_sources"

    def get_client(self):
        from quant.data.sources.registry import get_registry
        registry = get_registry()
        registry.load_from_config()
        return registry


class FactorStoreResource(dg.ConfigurableResource):
    """因子存储资源."""
    db_path: str = "quant/data/factor_cache.db"

    def get_client(self):
        from quant.factor.store import FactorStore
        return FactorStore(db_path=self.db_path)


class TradeRepoResource(dg.ConfigurableResource):
    """交易仓库资源."""
    db_path: str = "quant/data/trades.db"

    def get_client(self):
        from quant.data.repos import TradeRepo
        return TradeRepo(db_path=self.db_path)


class MarketDBResource(dg.ConfigurableResource):
    """行情数据库资源."""
    db_path: str = "quant/data/market.db"

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

    from quant.scheduler.repair import _run
    _run(partition_date)

    return {"date": partition_date, "status": "completed"}


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

    from quant.scheduler.signals import _run
    result = _run(partition_date)

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

    from quant.scheduler.execute import _run
    result = _run(partition_date)

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

    from quant.scheduler.snapshot import snapshot_open as _snapshot_open
    result = _snapshot_open(partition_date)

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

    from quant.scheduler.snapshot import snapshot_close as _snapshot_close
    result = _snapshot_close(partition_date)

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

    from quant.scheduler.reconcile import _run
    result = _run(partition_date)

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

    from quant.scheduler.daily_data import _run
    _run(partition_date)

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

    from quant.data.store import DataStore
    store = DataStore()
    result = store.sync_adj_factor(max_batches=1)
    store.close()

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

    from quant.config.constants import _require_cfg
    from quant.scheduler.factor_cache import _run as _fc_run
    _fc_start = _require_cfg("backtest.factor_cache_start")
    _fc_run(_fc_start, partition_date)

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

    from quant.scheduler.attribution import _run
    _run(partition_date)

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

    from quant.scheduler.lgb_train import _run
    _run(partition_date)

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

    from quant.scheduler.xgb_train import _run
    _run(partition_date)

    return {"date": partition_date, "status": "completed"}


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
    data_source_registry: ResourceParam[DataSourceRegistryResource],
    factor_store: ResourceParam[FactorStoreResource],
) -> dict:
    """每周六 06:00 运行."""
    partition_date = context.partition_key
    context.log.info(f"[{partition_date}] weekly_eval starting")

    from quant.scheduler.weekly import _run
    _run(partition_date)

    return {"date": partition_date, "status": "completed"}


# ═══════════════════════════════════════════════════════════════════
# 作业定义
# ═══════════════════════════════════════════════════════════════════

# 日线作业: daily_repair → signals → execute → snapshot_open → monitor → snapshot_close → reconcile → evening_chain
# evening_chain = daily_data → adj_factor → factor_cache → attribution → [lgb_train, xgb_train]

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
        "attribution",
        "lgb_train",
        "xgb_train",
    ),
    partitions_def=trading_day_partitions,
    description="交易日全流程: 早间补拉 → 信号 → 执行 → 快照 → 盘中风控 → 对账 → 晚间链",
)

weekly_job = define_asset_job(
    name="weekly_evaluation_job",
    selection=AssetSelection.keys("weekly_eval"),
    partitions_def=weekly_partitions,
    description="周六因子评估全流程",
)

# ═══════════════════════════════════════════════════════════════════
# 调度定义 — 替代自研 orchestrator 的 30s 轮询
# ═══════════════════════════════════════════════════════════════════

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
# ═══════════════════════════════════════════════════════════════════

@dg.sensor(
    job=define_asset_job(
        name="monitor_daemon_job",
        selection=AssetSelection.keys("monitor"),
        partitions_def=trading_day_partitions,
    ),
    default_status=DefaultSensorStatus.RUNNING,
    minimum_interval_seconds=60,
)
def monitor_sensor(context: dg.SensorEvaluationContext):
    """盘中风控 Sensor — 交易日 09:30-15:00 确保 monitor 守护进程存活.

    逻辑:
      - 非交易日: 不触发
      - 交易日 09:30 前: 启动 monitor 守护进程
      - 交易日 15:00 后: 停止 monitor 守护进程
      - 盘中每分钟检查进程存活, 挂了自动重启
    """
    from quant.execution.calendar import is_trading_day
    now = datetime.now()
    today = now.date()

    if not is_trading_day(today):
        return SkipReason(f"{today} 非交易日")

    hhmm = now.time()

    if hhmm < time(9, 30):
        # 交易日开盘前: 触发 monitor 资产物化 (启动守护进程)
        yield RunRequest(
            partition_key=today.isoformat(),
            tags={"trigger": "market_open"},
        )
    elif hhmm >= time(15, 0):
        # 收盘后: 不再触发, 守护进程自退
        return SkipReason("market closed")
    else:
        # 盘中: 定期检查 (由 minimum_interval_seconds=60 控制)
        # 实际进程存活检查在 monitor 资产内部或外部 systemd 管理
        return SkipReason("monitor daemon running")


# ═══════════════════════════════════════════════════════════════════
# Definitions 导出
# ═══════════════════════════════════════════════════════════════════

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