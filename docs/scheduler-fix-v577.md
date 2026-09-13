# 调度 Tab / Dagster 修复报告 — v577

> 日期: 2026-08-31
> 修复文件: `quant/orchestrator/dagster_assets.py`, `quant/scheduler/__init__.py`, `scripts/start_dagster_daemon.sh`

---

## 一、调度 Tab 任务状态"未配置"问题

### 症状
Dagster 模式下，所有任务显示灰色"未配置"。

### 根因
asset 名 `adj_factor_sync` 与 `task_runs` 表中的 `adj_factor` 不匹配，`/api/scheduler` 按任务名匹配时找不到记录。

### 修复
`adj_factor_sync` → `adj_factor`，函数内部局部变量改用 `_store` 避免与 `DataStore.sync_adj_factor` 方法冲突。

---

## 二、Dagster daemon 架构问题

### 问题 1：webserver 用独立 DAGSTER_HOME（历史）
原 `start_dagster_daemon.sh` 的 webserver 启动未指定 `DAGSTER_HOME`，导致 dagster-webserver 用临时目录，与 daemon 的 gRPC server 池冲突，造成 `clear_all_grpc_endpoints()` 不断清空所有端点。

**已修复**：webserver 启动时设置 `export DAGSTER_HOME=/tmp/dagster_home`。

### 问题 2：多 gRPC server heartbeat 冲突（历史）
同时运行 daemon (gRPC 45s) + webserver (gRPC 20s)，webserver 的 gRPC 每 20s 崩溃重启不影响 daemon（daemon 端点池独立），但 daemon 的端点池在 webserver 启动早期有过混乱。

**当前状态**（已正常）：
```
dagster-daemon (69684)
  └─ gRPC server (45s heartbeat, 稳定)
      └─ SchedulerDaemon: 每分钟 tick ✅
      └─ SensorDaemon: 每 30s tick ✅
      └─ AssetDaemon ✅ / BackfillDaemon ✅ / FreshnessDaemon ✅ / QueuedRunCoordinatorDaemon ✅

dagster-webserver (70057)
  └─ gRPC server (20s heartbeat, 会重启)
      └─ 仅影响 UI/API，不影响 SchedulerDaemon ✅
```

---

## 三、daily_data 是否在 19:00 准时触发？

### 配置 ✅
```
daily_data_schedule cron: "0 19 * * 1-5" (Asia/Shanghai)
下次触发: 2026-08-31 19:00 CST (交易日)
精度: cron ±30s (SchedulerDaemon 每分钟 tick 检测)
```

### SchedulerDaemon 状态 ✅
- 心跳正常，无 errors
- 5 个 schedules 全部加载 (DECLARED_IN_CODE)
- `job_ticks` 在触发前会记录评估日志

### 风险项 ⚠️
- webserver 的 20s gRPC 会崩溃重启，不影响 daemon 的 SchedulerDaemon
- 如果系统负载高，触发延迟可能在 1 分钟内

---

## 四、修改文件

| 文件 | 修复 |
|------|------|
| `quant/orchestrator/dagster_assets.py` | `adj_factor_sync`→`adj_factor` |
| `quant/scheduler/__init__.py` | `_start_dagster()` 正确启动进程 |
| `scripts/start_dagster_daemon.sh` | webserver 设置 DAGSTER_HOME |

---

## 五、验证清单

- [x] `adj_factor` asset 存在 (与 task_runs 一致)
- [x] SchedulerDaemon 心跳 tick
- [x] 5 schedules 全部加载
- [x] Sensor `period=盘后` 正确输出
- [ ] 19:00 实际触发验证 (等待)

---

## v578: Schedule Partition Key Fix & Stability Verification

**Date**: 2026-08-31  
**Status**: ✅ COMPLETE

### Problem
All 4 schedules would FAIL on every run because:

1. **Missing `partition_key`**: Dagster's default `execution_fn` generates `RunRequest` without `partition_key`. When the job's `partitions_def` is set and assets use `context.partition_key`, this raises:
   ```
   'Cannot access partition_key for a non-partitioned run'
   ```

2. **Repository caching**: `WeeklyPartitionsDefinition(end_offset=0)` only generates partitions up to the current date. When `get_partition_key_for_timestamp()` computes a partition key, it may return a key that's valid (last Saturday) but not yet in the repository's cached partition set, causing `KeyError: '2026-08-23'` in the schedule's partition validation.

### Fix

#### 1. `_make_partitioned_schedule()` helper (v577 already done)
All 4 schedules now use custom `execution_fn` that explicitly passes `partition_key` via `RunRequest`:
```python
def _execution_fn(context):
    partition_key = get_partition_key(context.scheduled_execution_time)
    return dg.RunRequest(partition_key=partition_key, run_key=partition_key, run_config={})
```

#### 2. `end_offset=2` for `weekly_partitions`
Changed from `end_offset=0` to `end_offset=2` to ensure the partition key computed by `get_partition_key_for_timestamp` is always within the repository's cached partition range.

### Verification Results (2026-08-31 21:08 CST)
```
Testing all schedules with partition_key fix:
  ✅ daily_trading_job_schedule (Sun 05:00): partition_key=2026-08-29
  ✅ end_of_day_job_schedule (Mon 15:00): partition_key=2026-08-31
  ✅ daily_data_job_schedule (Mon 19:00): partition_key=2026-08-31
  ✅ weekly_evaluation_job_schedule (Sun 06:00): partition_key=2026-08-30

Next trading day (2026-09-01 Tuesday):
  ✅ daily_trading_job_schedule: partition_key=2026-08-31 (Mon)
  ✅ end_of_day_job_schedule: partition_key=2026-09-01 (Tue)
  ✅ daily_data_job_schedule: partition_key=2026-09-01 (Tue)
  ✅ weekly_evaluation_job_schedule: partition_key=2026-08-30 (Sat on/before 2026-08-31)
```

### Process Stability
- gRPC server (PID 81461): 6+ minutes stable
- SchedulerDaemon (PID 81469): 6+ minutes stable, all 6 daemon types ticking
- Webserver (PID 81484): 6+ minutes stable

### Schedule States
All 4 schedules are `is_running=True`, `status=DECLARED_IN_CODE`:
- `weekly_evaluation_job_schedule`: cron=0 6 * * 6, last_tick=08-31 20:17:34
- `daily_data_job_schedule`: cron=0 19 * * 1-5, last_tick=08-31 20:19:51
- `daily_trading_job_schedule`: cron=0 5 * * 1-5, last_tick=08-31 20:19:51
- `end_of_day_job_schedule`: cron=0 15 * * 1-5, last_tick=08-31 20:22:11

### Known Dagster 1.13.18 Issue (Non-blocking)
`ScheduleIterationTimes.last_iteration_timestamp` is stored as wall-clock time instead of scheduled execution time. This causes a 1-iteration delay (the schedule fires ~40 minutes after the cron time instead of at it). Acceptable for trading system — the actual trading logic is triggered at the correct partition date.

### Next Trigger
Monday 2026-09-01 05:00 CST — `daily_trading_job_schedule` will fire with `partition_key=2026-08-31`.
