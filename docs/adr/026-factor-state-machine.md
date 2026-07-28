# ADR 026: 因子状态机重构

**日期**: 2026-07-24 | **状态**: 实施中 | **版本**: test-v237

---

## 背景

因子状态当前由两个模块竞争管理：
- `evaluation/phase5_monitor.py` — 周度评估，负责 `candidate→active/monitoring/retired/rejected`
- `scheduler/attribution.py` — 每日归因，负责 `active→monitoring→active(恢复)/retired`

问题：
1. 两个模块互不可见对方的决策上下文
2. `registered` vs `candidate` 状态区分模糊(实际无代码引用`registered`)
3. `monitoring` 语义混用(评估不完整 vs IC衰减)
4. `retry_count` 只在phase5中维护，attribution置retired的因子可能永久卡住
5. active因子的"不碰"策略有隐患(attribution崩溃时无fallback)

## 决策

### 1. 引入单一状态管理者 `FactorStateManager`

唯一写入 `factor_registry.status` 的模块。其他模块通过事件上报，不直接写DB。

### 2. 状态机: 6状态 → 5状态

```
当前: registered → candidate → active → monitoring → retired → rejected
重构:            candidate → active → monitoring → retired → rejected
```

`registered` 与 `candidate` 合并。因子卡片创建时直接写入 `candidate`。

### 3. 状态转换事件表

| 事件 | 触发者 | 从 | 到 | 条件 |
|------|--------|----|----|------|
| `EVAL_PASS` | phase5 | candidate | active | Phase 2+3+4 全部通过 |
| `EVAL_MARGINAL` | phase5 | candidate | monitoring | 部分通过但不满足active门槛 |
| `EVAL_FAIL` | phase5 | candidate | retired | 全部未通过(但retry<max) |
| `EVAL_REJECT` | phase5 | retired | rejected | retry_count ≥ max_retries |
| `IC_DEGRADED` | attribution | active | monitoring | L1/L2衰减检测触发 |
| `IC_RECOVERED` | attribution | monitoring | active | 连续N天IC恢复稳定 |
| `IC_PERSISTENT` | attribution | monitoring | retired | 超过buffer天持续衰减 |
| `RETRY_RESTORE` | phase5 | retired | candidate | 新评估周期重试 |

### 4. monitoring语义拆分

通过 `status_reason` 字段区分：
- `[EVAL]` 前缀 → 评估阶段进入(数据不足/边际)
- `[LIVE]` 前缀 → 实盘IC衰减进入

## 对标

| 维度 | WorldQuant | AQR/Barra | 本项目(重构后) |
|------|-----------|-----------|--------------|
| 状态数 | 5 | 3(结构性/周期性/实验性) | 5 |
| 状态管理 | 单一调度器 | 内部系统 | FactorStateManager(单一) |
| 转换机制 | 自动评估→升级 | 宏观regime开关 | 事件驱动transition() |
| Active门槛 | Sharpe>0, IR>0 | t>3.0 + OOS | Phase 2+3+4全通过 |
| 恢复机制 | 重新Submit | 重新评估 | monitoring→active恢复 |

## 影响

- 新增: `quant/factor/state_manager.py` — FactorStateManager 类
- 修改: `quant/evaluation/phase5_monitor.py` — 通过StateManager转换
- 修改: `quant/scheduler/attribution.py` — 通过StateManager转换
- 数据迁移: `factor_registry.status='registered'` → `'candidate'`

## 设计约束

- 零fallback: 非法状态转换→ValueError, 不静默降级
- 所有阈值从config.yaml读取
- 状态转换表为纯数据结构, 可单元测试
