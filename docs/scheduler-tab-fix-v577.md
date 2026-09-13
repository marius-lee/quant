# 调度 Tab 修复报告 — v577

> 日期: 2026-08-31
> 范围: `quant/orchestrator/dagster_assets.py`
> 背景: 修复 DAGSTER 模式下调度任务状态"未配置"问题

---

## 问题描述

### 症状
Dagster 模式下，`/api/scheduler` 返回的所有任务状态显示为"未配置"（灰色），即使 Dagster daemon 正在正常运行。

### 根因

**两套编排体系的任务名不匹配**：

| 组件 | 任务名 |
|------|--------|
| `task_runs` 表 (数据来源) | `signals`, `execute`, `monitor`, `reconcile`, `adj_factor`, `duckdb_sync`, `factor_cache`, `attribution`, `lgb_train`, `xgb_train`, `weekly_eval`, `daily_repair`, `snapshot_open`, `snapshot_close`, `daily_data` |
| `status.py` (Legacy 任务清单) | 同上 |
| `dagster_assets.py` (Dagster assets) | **错误用了 `adj_factor_sync`** |

`adj_factor_sync` 不在 `task_runs` 表中，`/api/scheduler` 的任务匹配逻辑：

```python
# app.py api_scheduler()
run = db_runs.get(key)   # key = "adj_factor_sync"
# db_runs 中没有 "adj_factor_sync" → None
# → 进入 else 分支 → "未配置" 徽章
```

### 修复方案

将 `dagster_assets.py` 中的 asset 名 `adj_factor_sync` **改回 `adj_factor`**，与 `task_runs` 表保持一致。

函数内部避免与 `DataStore.sync_adj_factor` 方法同名冲突，改用局部变量 `_store`：

```python
def adj_factor(context, daily_data, data_source_registry):
    # v577 fix: asset 名保持 adj_factor (与 task_runs/task_log 一致)
    from quant.data.store import DataStore
    _store = DataStore()
    result = _store.sync_adj_factor(max_batches=1)
    _store.close()
```

### 验证

```python
# 本地 definitions 验证
keys = ['adj_factor', 'attribution', 'daily_data', ...]  # 共 15 个
"adj_factor" in keys: True
"adj_factor_sync" not in keys: True
```

Dagster 重启后 daemon 日志无错误，Sensor 正常响应 (`period=盘后`)。

---

## 归档

- 修复文件: `quant/orchestrator/dagster_assets.py`
- 相关文件: `quant/scheduler/status.py`（无需修改）
- 相关文件: `web/app.py` `api_scheduler()`（无需修改）
