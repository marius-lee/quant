# 冒烟测试回测业务流程代码错误分析报告

**生成日期**: 2026-08-01  
**分析对象**: 项目冒烟测试日志（`logs/backtest.log`、`logs/backtest.log.2026-07-31`、`logs/quant.log`、`logs/cron.log`）  
**分析范围**: 回测业务流程相关的代码错误（不改代码，仅输出报告）  
**结论摘要**: 冒烟测试暴露出多个严重代码错误，导致回测实际零成交却报告“成功”，因子评估流程崩溃，数据类型错误造成大规模日度失败。当前代码不可直接用于生产决策。

---

## 一、关键发现总览

| 等级 | 问题 | 影响 | 证据位置 |
|---|---|---|---|
| 🔴 严重 | `pipeline.py` 在 `suppress_push=True` 时仍引用未导入的 `broker` | 回测每日失败，零成交，但返回虚假 0% 收益 | `logs/backtest.log` 2026-08-01 全天 |
| 🔴 严重 | 回测结果持久化逻辑不检查 `errors` 计数 | 失败 22/22 天仍写入 `backtest_runs` 并报告成功 | `logs/backtest.log` 末尾 |
| 🔴 严重 | `phase5_monitor.py` 使用未定义变量 `f_repo` | 因子状态同步流程崩溃 | `logs/backtest.log` 08:45:54 |
| 🟠 高 | 因子计算 `np.log` 遇到 `object`/`float` 数据类型失败 | 2024-09-30 起连续日度失败 | `logs/backtest.log.2026-07-31` |
| 🟠 高 | 因子状态机事件与状态不匹配 | `archived`/`probation` 状态收到非法事件 | `logs/backtest.log` 多次 |
| 🟠 高 | Phase 3 无因子通过，Phase 4 被跳过 | 策略无可用因子 | `logs/backtest.log` 08:45:54 |
| 🟡 中 | OOS 衰减严重，大量因子 IS→OOS 衰减 <50% | 过拟合风险高 | `logs/quant.log` / `logs/backtest.log` |
| 🟡 中 | 运行时并发/配置/网络问题 | cron 任务失败、数据库锁、配置缺失 | `logs/cron.log` |

---

## 二、详细错误分析

### 🔴 错误 1: `pipeline.py` 未受保护的 `broker` 引用（回测零成交根因）

#### 现象
`logs/backtest.log` 2026-08-01 全天（trace_id `2747ca99bbc3`、`3dabf36dd833`、`28247e04eef5`）出现大量：

```json
{"ts": "2026-08-01T10:35:28", "level": "ERROR", "module": "quant.backtest.loop",
 "msg": "backtest 2026-06-01: day failed (1 total): cannot access local variable 'broker' where it is not associated with a value"}
```

最终回测结果：
```json
{"ts": "2026-08-01T11:08:39", "level": "INFO", "module": "quant.backtest.loop",
 "msg": "backtest done in 116.3s: CAGR=0.0%, Sharpe=0.0, MDD=0.0%, avg_signals/day=0.0, errors=22"}
```

#### 根因定位
`quant/pipeline.py` 中，`broker` 仅在 `not suppress_push` 时导入：

```python
# pipeline.py ~line 87
if not suppress_push:
    from web.state_broker import broker
    broker.update({"status": "signals_started", ...})
```

但在 regime 分支（~line 268）存在未受保护的调用：

```python
if regime_label is not None:
    from quant.regime.detector import get_regime_sizing
    _sizing = get_regime_sizing(regime_label)
    broker.update({"regime": regime_label, ...})   # ← 未判断 suppress_push
```

回测调用 `generate_signals(suppress_push=True, ...)`，且 `backtest/loop.py` 注入了 point-in-time regime（`kwargs["regime_label"]` 非空），于是每次进入该分支都会触发 `NameError`。

#### 业务影响
- 回测主循环的 `try/except` 捕获该异常后跳过当日交易，但仍把上一日权益曲线重复写入。
- 23 个交易日全部失败，实际成交 0 笔，但返回的 metrics 显示 CAGR=0.0%、Sharpe=0.0、MDD=0.0%，极具误导性。
- 后续的 `diagnose()` 基于零交易数据建议丢弃几乎全部因子，进一步污染因子注册表。

#### 修复方向（不改代码，仅建议）
1. 将 `broker.update(...)` 包裹在 `if not suppress_push:` 内。
2. 或在函数顶部统一初始化一个 `NullBroker` 占位对象，避免分支判断遗漏。
3. 回测循环应在 `errors == len(trading_days)-1` 时直接标记回测失败，不持久化结果。

---

### 🔴 错误 2: 回测结果持久化不校验错误计数

#### 现象
尽管 `errors=22`（等于实际交易日数），`backtest/loop.py` 仍执行：

```json
{"ts": "2026-08-01T11:08:39", "level": "INFO", "module": "quant.backtest.loop",
 "msg": "backtest: result persisted to backtest_runs"}
```

#### 根因
`run_backtest` 返回前没有根据 `errors` 或 `avg_signals/day` 判断回测是否有效。只要流程走到最后，就写入 `backtest_runs` 并打印“BACKTEST END”。

#### 业务影响
- 下游系统（web 展示、策略筛选、自动调参）会把这个全失败的运行当作有效结果。
- 历史绩效数据库被污染，后续分析不可靠。

#### 修复方向
- 增加失败阈值：当 `errors / trading_days > 0.1` 或 `avg_signals/day == 0` 时，返回 `{"error": ...}` 并不写入 `backtest_runs`。
- 日志中应显式输出 `FAILED` 而非 `BACKTEST END`。

---

### 🔴 错误 3: `phase5_monitor.py` 未定义 `f_repo`

#### 现象
```json
{"ts": "2026-08-01T08:45:54", "level": "ERROR", "module": "quant.scheduler.weekly",
 "msg": "[2026-08-01] eval_phase5 failed: name 'f_repo' is not defined"}
```

Traceback 指向 `quant/evaluation/phase5_monitor.py:206`：

```python
current = f_repo.get_factor_by_name(name)
          ^^^^^^
NameError: name 'f_repo' is not defined
```

#### 根因
`phase5_monitor.py` 在 `sync_factor_status()` 中直接使用 `f_repo`，但该变量未在方法内定义，也未作为参数传入。可能是重构时遗漏，或应为 `FactorRepo()` 实例。

#### 业务影响
- 每周因子状态同步（退休/监控/恢复）完全失败。
- 因子生命周期管理停滞， retired 因子无法被正确 retry 或 reject。

---

### 🟠 错误 4: 因子计算 `np.log` 数据类型错误

#### 现象
`logs/backtest.log.2026-07-31` 从 2024-09-30 起连续出现：

```json
{"ts": "2026-07-31T00:03:47", "level": "ERROR", "module": "quant.backtest.loop",
 "msg": "backtest 2024-09-30: day failed (181 total): loop of ufunc does not support argument 0 of type float which has no callable log method"}
```

累计数百个交易日失败。

#### 根因定位
错误来自 numpy ufunc `np.log` 作用到非 ndarray 的 `object`/`float` 数据上。项目中多处使用 `np.log(close)`、`np.log(volume)`、`np.log(close / open_)` 等：

- `quant/factor/compute/_primitives.py:42`：`prims["log_ret"] = np.log(close).diff()`
- `quant/factor/compute/price/_momentum.py:18` / `:640`
- `quant/factor/compute/price/_alternative.py:149` / `:153`
- `quant/factor/compute/alpha101.py:110`

当 `close`/`volume` 某列在特定日期后变为 `object` dtype（例如 DB 中混入了字符串、NULL 或不同精度数值），`np.log` 会抛出该错误。

#### 业务影响
- 2024-09-30 之后的历史回测大面积失败，无法获得完整绩效。
- 可能是数据清洗/复权因子同步引入的类型污染。

#### 修复方向
1. 在 `precompute_primitives` 和因子函数入口强制 `close = close.astype(float)`。
2. 对 `np.log` 调用前做 `pd.to_numeric(..., errors='coerce')`。
3. 在数据入库层统一价格/成交量字段为 `REAL` 类型并校验。

---

### 🟠 错误 5: 因子状态机事件非法

#### 现象
`logs/backtest.log` 多次出现：

```json
{"ts": "2026-08-01T09:31:59", "level": "WARNING", "module": "quant.evaluation.phase5",
 "msg": "sync_factor_status: rsi_rev_14d EVAL_MARGINAL failed: 非法状态转换: archived --(EVAL_MARGINAL)--> ? (允许的事件: ['RETRY_RESTORE'])"}

{"ts": "2026-08-01T09:31:59", "level": "WARNING", "module": "quant.evaluation.phase5",
 "msg": "sync_factor_status: turnover_rev_5d EVAL_MARGINAL failed: 非法状态转换: probation --(EVAL_MARGINAL)--> ? (允许的事件: ['IC_RECOVERED', 'IC_PERSISTENT', 'FACTOR_REDUNDANT', 'DATA_SOURCE_DEAD'])"}
```

以及：

```json
{"ts": "2026-08-01T09:31:59", "level": "WARNING", "module": "quant.evaluation.phase5",
 "msg": "sync_factor_status: hl_volume_20d EVAL_REJECT failed: 非法事件: 'EVAL_REJECT' (允许: ['DATA_SOURCE_DEAD', ...])"}
```

#### 根因
`phase5_monitor.py` 生成的事件（`EVAL_MARGINAL`、`IC_PERSISTENT`、`EVAL_REJECT`）与 `factor` 状态机当前状态允许的事件不匹配。`archived` 状态只允许 `RETRY_RESTORE`，但代码仍向 retired/archived 因子发送 `EVAL_MARGINAL`/`IC_PERSISTENT`。

#### 业务影响
- 因子状态转换大量失败，监控/恢复逻辑失效。
- `retry_count` 与状态不同步，可能导致因子被重复 retired 或永远无法恢复。

---

### 🟠 错误 6: Phase 3 无因子通过，Phase 4 跳过

#### 现象
```json
{"ts": "2026-08-01T08:45:54", "level": "WARNING", "module": "quant.evaluation.phase4",
 "msg": "No factors from Phase 3. Skipping Phase 4."}
```

Phase 3 结果：
```json
{"ts": "2026-08-01T08:45:54", "level": "INFO", "module": "quant.evaluation.phase3",
 "msg": "Phase 3 complete (149.6s). 0 kept, 1 marginal, 1 dropped (total: 2 candidates)"}
```

#### 根因
Phase 2 仅 2 个因子通过（`turnover_rev_5d`、`vol_price_corr_10d`），Phase 3 中 `vol_price_corr_10d` OOS 衰减 136% 被 drop，`turnover_rev_5d` OOS ICIR 仅 0.197 被标为 marginal，无因子进入 Phase 4。

#### 业务影响
- 当前因子池无法产生有效 Alpha，回测/实盘均缺乏信号来源。
- 需要重新审视因子定义、方向、IC 计算正确性（参见 `BACKTEST-FACTOR-ANALYSIS-2026-08-01.md`）。

---

### 🟡 错误 7: OOS 衰减严重

#### 现象
`logs/quant.log` / `logs/backtest.log` 中大量：

```json
{"ts": "2026-08-01T10:34:53", "level": "WARNING", "module": "quant.scheduler.oos_verify",
 "msg": "[2026-06-01] OOS decay alert: 6/16 below 50%"}

{"ts": "2026-08-01T10:34:53", "level": "WARNING", "module": "quant.scheduler.oos_verify",
 "msg": "  amihud_250d: IS_IR=+0.3029 → OOS_IR=+0.0314 (ratio=0.10)"}
```

#### 根因
大量因子在样本内有效，但样本外 IR 大幅衰减。这与错误 4 的数据类型问题、因子计算错误（如 `reversal_5d` 实为动量，见因子分析报告）共同导致。

#### 业务影响
- 即使回测跑通，结果也是过拟合，无法复现到实盘。

---

### 🟡 错误 8: 运行时运维问题

`logs/cron.log` 中可见：

| 时间 | 错误 | 影响 |
|---|---|---|
| 07-24 | `KeyError: 'config.yaml missing required key: monitor.max_drawdown_pct'` | 监控模块无法启动 |
| 07-24 / 07-27 / 07-29 | `sqlite3.OperationalError: database is locked` | 并发任务互相阻塞，数据同步/任务日志失败 |
| 07-24 / 07-30 | `RuntimeError: task_log.finish(...): no running row found` | 任务状态机不一致 |
| 07-29 | `TypeError: _require_cfg() takes 1 positional argument but 2 were given` | `signals` 阶段崩溃，当日无信号 |
| 07-30 | `RuntimeError: step 3: factor_cache miss for 2026-07-29` | 因子缓存未物化，execute 阶段拒绝执行 |
| 07-30 | `NameError: name '_bs_code' is not defined` | turnover 回填代码错误 |

---

## 三、回测业务流程问题总结

### 3.1 冒烟测试实际结果

以 `scripts/smoke_test.sh` 参数（2026-07-01 → 2026-07-31，capital=5000，universe_size=300，retrain_freq=0）运行的最新一次回测：

| 指标 | 日志值 | 真实含义 |
|---|---|---|
| 交易日 | 23 | 23 |
| 失败日 | 22 | 22（几乎全部） |
| 日均信号 | 0.0 | 零成交 |
| CAGR | 0.0% | 未交易，现金无变化 |
| Sharpe | 0.0 | 未交易 |
| MDD | 0.0% | 未交易 |
| 是否持久化 | 是 | 错误：应判定为失败 |

### 3.2 流程图上的断点

```
数据准备 → Phase1/2/3/4 评估 → 生成信号 → 执行回测 → 计算指标 → 诊断 → 持久化
              │                    │           │          │       │
              ▼                    ▼           ▼          ▼       ▼
        Phase5 f_repo 未定义   broker 引用缺失  全失败   零交易  仍写入 DB
        状态机事件非法
```

### 3.3 与上一版日志的对比

- `backtest.log.2026-07-31` 的主要错误是 `np.log` 数据类型错误，说明 2024-09-30 后的数据有问题。
- `backtest.log` 2026-08-01 的主要错误变成 `broker` 未定义，说明最新代码引入了新的 regression，且掩盖了旧的数据问题（因为回测在生成信号阶段就失败了，未到达因子计算）。

---

## 四、修复优先级建议

### P0（立即修复，否则回测不可信）
1. 修复 `quant/pipeline.py` ~line 268 的 `broker.update` 未受 `suppress_push` 保护问题。
2. 在 `quant/backtest/loop.py` 返回前校验 `errors` 和 `avg_signals/day`，全失败时不持久化并返回错误。
3. 修复 `quant/evaluation/phase5_monitor.py` 的 `f_repo` 未定义问题。

### P1（短期修复）
4. 统一因子计算入口的数据类型转换，解决 `np.log` 的 object/float 错误。
5. 修正因子状态机事件映射，确保 `EVAL_MARGINAL`/`IC_PERSISTENT`/`EVAL_REJECT` 只发往合法状态。
6. 增加配置缺失的兜底默认值或启动校验（`monitor.max_drawdown_pct` 等）。

### P2（中期重构）
7. 解决 SQLite `database is locked` 并发问题（连接池、读写分离、或迁移到专用时序数据库）。
8. 建立 paper trading / 冒烟测试的断言机制：不仅检查无异常，还要检查 `errors=0` 且 `avg_signals/day > 0`。
9. 修复底层因子计算 bug（参见 `BACKTEST-FACTOR-ANALYSIS-2026-08-01.md`），否则 Phase 3 将持续无可用因子。

---

## 五、结论

冒烟测试日志清晰表明：**当前回测业务流程存在致命 regression**。最新一次回测在 23 个交易日中失败 22 天，实际零成交，但系统仍将其作为有效结果持久化。与此同时，因子评估与状态同步流程也处于半崩溃状态。

**在修复 P0 问题之前，任何基于该回测框架的策略绩效、因子筛选结果都不可信。** 建议先跑通最小冒烟测试（ asserts `errors==0` 且 `avg_signals/day>0` ），再逐步修复 P1/P2 问题。
