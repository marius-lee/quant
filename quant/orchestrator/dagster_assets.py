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
