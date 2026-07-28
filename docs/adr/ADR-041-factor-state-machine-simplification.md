# ADR-041: 因子状态机简化 (方案 B)

**日期**: 2026-07-28
**状态**: ✅ 已实施
**依赖**: ADR-040 (因子覆盖补充)

---

## 决策

将因子状态机从 5 状态简化为 4 状态，引入 rolling t-test 替代硬时间阈值。

## 状态变更

```
原状态             新状态            说明
───────            ───────           ────
candidate      →   evaluating        待评估因子
active         →   active            实盘信号, 完整权重
monitoring     →   probation         IC 衰减观察, 衰减权重
retired        →   archived          归档 (IC 持续衰减)
rejected       →   archived          归档 (数据源死亡)
```

## 新状态机

```
                    ┌──────────┐
           ┌───────│evaluating│◄──────────┐
           │       └────┬─────┘            │
      EVAL_PASS    EVAL_MARGINAL    RETRY_RESTORE
           │       ┌────┴─────┐            │
           ▼       ▼          │            │
       ┌──────┐ ┌──────────┐  │            │
       │active│ │probation │  │            │
       └──┬───┘ └────┬─────┘  │            │
    IC_↓    IC_↑     IC_↓     │            │
  DEGRADED RECOVERED PERSIST  │            │
       │       ┌────┴──────┐  │            │
       │       ▼           ▼  │            │
       │   ┌──────────────────┐│            │
       └──►│    archived      │◄───────────┘
           │ (status_reason   │
           │  区分 IC衰减/    │
           │  数据源死亡)     │
           └──────────────────┘
              DATA_SOURCE_DEAD (active/probation → 直接归档)
```

## 转换表

| from | event | to |
|---|---|---|
| evaluating | EVAL_PASS | active |
| evaluating | EVAL_MARGINAL | probation |
| evaluating | EVAL_FAIL | archived |
| active | IC_DEGRADED | probation |
| active | DATA_SOURCE_DEAD | archived (新增) |
| probation | IC_RECOVERED | active |
| probation | IC_PERSISTENT | archived |
| probation | DATA_SOURCE_DEAD | archived (新增) |
| archived | RETRY_RESTORE | evaluating |

## 关键改进

### 1. rolling t-test 替代硬时间阈值

```
旧: if updated_at < buffer_cutoff (MONITORING_BUFFER_DAYS=15):
        probation → archived

新: rolling IC t-statistic < 1.0 for N days:
        probation → archived
```

### 2. 快速降级路径

`DATA_SOURCE_DEAD` 事件: active/probation 因子数据源死亡时直接归档，跳过观察期。

### 3. 统一状态变更入口

所有状态转换统一走 `FactorStateManager.transition()`，不再有直接 SQL UPDATE。

### 4. 因子池

```
using       = active + probation          (实盘信号生成)
backtesting = evaluating + probation + archived  (回测评估池)
```

## 变更清单

| 文件 | 变更 |
|---|---|
| `quant/data/repos/factor_repo.py` | VALID_STATUSES 更新 |
| `quant/factor/state_manager.py` | 状态表 + check方法 + pools 重构 |
| `quant/factor/compute/_registry.py` | using/backtesting pools 重命名 |
| `quant/scheduler/attribution.py` | D1-D3 重构: probation + rolling t-test + fsm.transition |
| `quant/factor/stats_cache.py` | monitoring→probation |
| `quant/alpha/model.py` | monitoring→probation 权重衰减分支 |
| `factor_registry` (market.db) | DB 迁移: candidate→evaluating, monitoring→probation, retired+rejected→archived |

## 测试

- 221 测试全部通过 ✅
