<!-- 因子状态机 — 业务逻辑参考文档 -->
<!-- 最后更新: 2026-07-24 | 基于全量代码审查 -->

# 因子状态机

## 一、六种有效状态

```
VALID_STATUSES = {"registered", "candidate", "active", "monitoring", "retired", "rejected"}
```

来源: [factor_repo.py:43](/Users/mariusto/project/quant/quant/data/repos/factor_repo.py:43)

| #   | 状态         | 简称   | 含义                                   | 谁有权操作            |
|-----|-------------|--------|----------------------------------------|-----------------------|
| 1   | registered  | 已注册 | 因子代码写好了，但还没参与过任何评估     | 开发阶段               |
| 2   | candidate   | 候选   | 通过诊断预筛选，等待正式评估             | Phase 2 诊断           |
| 3   | retired     | 退役   | 曾在 active 但因 IC 衰减被监控模块降级   | 实盘监控降级            |
| 4   | rejected    | 淘汰   | 评估不通过 (IC 太低/CPCV 失败/Sharpe 不够) | Phase 2-3-4 评估     |
| 5   | active      | 有效   | 通过全部评估，用于实盘信号生成            | Phase 5 sync 提升      |
| 6   | monitoring  | 观察   | 有微弱信号但不足以交易 / IC 暂降未到退役线 | Phase 3/归因降级       |

## 二、虚拟查询别名

这两个不是数据库状态，是 `_resolve_statuses()` 定义的**查询过滤器**。
来源: [_registry.py:8-27](/Users/mariusto/project/quant/quant/factor/compute/_registry.py:8)

| 别名        | 映射到                                          | 用途               |
|------------|------------------------------------------------|--------------------|
| `using`    | `('active', 'monitoring')`                     | 实盘信号生成         |
| `backtesting` | `('registered', 'candidate', 'monitoring', 'retired')` | 回测评估池 |

```python
def _resolve_statuses(status_filter):
    if status_filter == 'using':
        # active + monitoring: monitoring 以衰减权重参与实盘信号生成
        # 来源: Grinold & Kahn (1999) Ch.6 Eq.6.16 — w_k ∝ IC_k/σ²_k
        #       monitoring 因子权重由 |IC_5d| / |IC_60d| 连续比例决定
        return ('active', 'monitoring')
    if status_filter == 'backtesting':
        # 所有非 active/非 rejected 的因子
        # 来源: WorldQuant WebSim — 回测池包含注册/候选/退役/监测因子
        #       rejected 永久排除 (数据源死亡)
        #       active 不参与回测 (已认证的线上因子)
        return ('registered', 'candidate', 'monitoring', 'retired')
    return (status_filter,)
```

## 三、状态流转闭环

### 3.1 实盘交易业务流程

```
                    ┌──────────┐
                    │registered│ ← 新因子注册
                    └────┬─────┘
                         │ Phase 5 评估
              ┌──────────┼──────────┐
              ▼ pass     ▼ marginal │ fail
         ┌────────┐ ┌───────────┐  │
         │ active │ │monitoring │  │
         └───┬────┘ └─────┬─────┘  │
             │             │        │
    IC衰减    │    IC恢复    │ 持续衰减│
    (D1)     │    (D2)     │ (D3)   │
      ┌──────┘   ┌─────────┘   ┌───▼──────┐
      ▼          ▼             ▼          │
  ┌───────────┐          ┌─────────┐      │
  │ monitoring│          │ retired │      │
  └───────────┘          └─────────┘      │
       │                       │          │
       │ IC恢复(D2)             │ 无自动恢复│
       ▼                       │          │
  ┌────────┐                    │          │
  │ active │                    │          │
  └────────┘                    │          │
                                ▼          ▼
                          ┌──────────┐
                          │ rejected │ ← 评估失败, 无自动恢复
                          └──────────┘
```

#### 状态变更代码位置

| 变更               | 触发条件                                                         | 代码位置 |
|-------------------|-----------------------------------------------------------------|---------|
| active → monitoring | L1: 滚动IC偏离 > IC_DEGRADATION_THRESHOLD, L2: OOS_IR/IS_IR < OOS_WARNING_DECAY | [attribution.py D1:213] |
| monitoring → active | OOS_IR/IS_IR 恢复 > OOS_RECOVERY_THRESHOLD, 且稳定 PROMOTION_STABILITY_DAYS 天 | [attribution.py D2:243] |
| monitoring → retired | 持续衰减超过 MONITORING_BUFFER_DAYS 天 | [attribution.py D3:261] |
| registered → active/monitoring/rejected | Phase 5 评估: 通过→active, 部分通过→monitoring, 失败→rejected | [phase5_monitor.py:423-450] |

**retired → monitoring 恢复**: 不存在。attribution D2 只查询 `monitoring` 状态的因子，不查 `retired`。retired 目前在代码中没有自动恢复路径。

### 3.2 回测业务流程

#### 冒烟测试

- **目标**: 快速验证流程是否通畅，尽快发现错误
- **因子池 (按设计)**: `active` — 只测已认证的有效因子。冒烟测试目标是尽快发现流程错误，因子越少越快。active 已通过全部评估，信号质量已知。
- **因子池 (代码现状)**: 未显式传参，默认 `factor_status_filter="backtesting"` [loop.py:142-143]，会拉入全部非 active/非 rejected 因子，与设计意图不符。
- **股票数**: 10 只 (`backtest.smoke.universe_size`) [loop.py:179]
- **时长**: ~22 日历天 ≈ ~14 交易日 (`end_date - 1 month`) [loop.py:178]
- **IC 窗口**: `ic_lookback=20` 天（冒烟测试专用，覆盖默认 120）[smoke_test_v279.py:58]

#### 正式回测

- **目标**: 评估所有非 active 因子的真实表现
- **因子池**: `backtesting` = `registered + candidate + monitoring + retired` [loop.py:143 默认值]
- **股票数**: 全量 (`backtest.universe_size` → None → 0=全量) [loop.py:190-191]
- **时长**: 12 个月 (默认 `end_date - 12 months`) [loop.py:187]
- **active 不参与回测** — 已经认证的线上因子不需要重新评估

## 四、所有调用方汇总

| 调用方 | status_filter | 含义 |
|-------|--------------|------|
| `attribution.py:116` | `"using"` | 实盘归因 — OOS walk-forward 验证 |
| `factor_attribution.py:48` | `"using"` | 因子 PnL 归因 |
| `factor_attribution.py:117` | `"using"` | 因子 PnL 归因内部 oos_verify |
| `crowdedness.py:52` | `"using"` | 因子拥挤度检测 |
| `_dispatch.py:37-38` | `"using"` | 因子计算派发 |
| `_registry.py:31,51` | 默认 `"using"` | load_active_*_factors |
| `backtest loop.py:143` | 默认 `"backtesting"` | 回测 (正式+冒烟共用) |
| `stats_cache.py:99,315` | 传入 `"backtesting"` 或 None | IC 计算缓存 |
| `phase7_wf.py:204` | `"active"` | Walk-forward OOS 临时激活 |

## 五、web 页面显示

`/api/factors` 路由 [web/app.py:127-172]:

```python
stats["n_active"] = dist.get("active", 0)        # ← DB 实际 key
stats["n_candidate"] = dist.get("candidate", 0)
stats["n_monitoring"] = dist.get("monitoring", 0)
stats["n_rejected"] = dist.get("rejected", 0)
stats["n_retired"] = dist.get("retired", 0)
```

**"有效因子"显示 `n_active`** — 对应 `active` 状态。当 attribution D1 把所有 active 降级后，这个数就是 0。
monitoring 状态的因子虽然也参与信号生成（`using = active + monitoring`），但在页面计数里不算"有效"。

## 六、配置参数

来源: config.yaml `oos_verify` / `attribution` / `backtest` 段

| 参数 | 值 | 作用 |
|-----|---|------|
| `oos_verify.train_window_days` | 60 | OOS 验证训练窗口 |
| `oos_verify.test_window_days` | 10 | OOS 验证测试窗口 |
| `oos_verify.decay_warn_threshold` | 0.5 | 衰减预警: OOS_IR/IS_IR 低于此值告警 |
| `attribution.ic_rolling_window` | config 读取 | 滚动 IC 监控窗口 |
| `attribution.ic_degradation_threshold` | config 读取 | IC 偏离阈值 |
| `attribution.oos_warning_decay` | config 读取 | OOS/IS 衰减阈值 |
| `attribution.oos_recovery_threshold` | config 读取 | OOS/IS 恢复阈值 |
| `attribution.monitoring_buffer_days` | config 读取 | monitoring→retired 缓冲天数 |
| `attribution.promotion_stability_days` | config 读取 | monitoring→active 稳定性天数 |
| `backtest.smoke.universe_size` | 10 | 冒烟测试股票数 |
| `backtest.min_trading_days` | config 读取 | 回测最少交易日 |
| `backtest.diagnosis_ic_window` | config 读取 | 回测 IC 计算窗口 |
