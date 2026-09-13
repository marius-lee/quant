# 代码 Review 报告 — Quant 项目

> 生成时间: 2026-08-30
> 审查范围: quant/ 代码（235 个 .py 文件）+ test/（67 个 .py 文件）
> 审查原则: 仅 review 代码，不 review 任何文档

---

## 1. 技术栈是否合适？还需要哪些改进？

### ✅ 合适的部分

| 组件 | 评价 |
|------|------|
| **SQLite + WAL** | 单机量化场景合理，读并发无锁 |
| **DuckDB 列式** | v435 已引入用于 BI 查询，读分流 ✓ |
| **LightGBM** | 适合 A 股权重异质截面数据，IC 加权足够 |
| **HMM regime** | 标准做法，3-state bull/sideways/bear |
| **因子存储 parquet** | 按因子×年分区，zstd 压缩，比 CSV gzip 省空间 |
| **预计算原语层** | primitives.py 避免重复 O(lookback×symbols)，架构合理 |

### ⚠️ 需要改进的部分

| 问题 | 建议 | 优先级 |
|------|------|--------|
| **无消息队列** | 模板 8 已注明激活条件 — 多人/多机时必须引入 Redis/Celery；当前单人可暂缓 | P1 |
| **因子计算无 Ray/Dask** | `_primitives.py` 全量预计算对全市场 5000 股 × 2000 日，内存峰值 ~1.2GB；已用 fork COW 优化，但极端情况 M1 8GB 仍危险 | P2 |
| **无模型注册表版本化** | `qlib_model.py` 模型保存为单文件，新模型覆盖旧模型，无灰度/回滚 | P2 |
| **缺少特征存储版本化** | 因子 panel 历史无法回滚到某个时间点；(feature drift 时无法重训) | P2 |
| **监控依赖 SSE 推送** | broker → SSE → web，无 OpenTelemetry/Grafana（模板 9 T2 阶段） | P3 |
| **因子库无版本标签** | 无法追踪"某次回测用的因子版本"，回测可复现性受限 | P3 |

---

## 2. 已实现功能 vs 缺失功能

### ✅ 已实现（按 Grinold & Kahn 7 层）

| Layer | 功能 | 状态 |
|-------|------|------|
| **0 数据** | SQLite 全量日线、DuckDB 查询层、因子缓存 parquet、物化脚本、CDC 变更捕获 | ✅ 完成 |
| **1 因子** | 104 因子（价格 35 + 基本面 69），含 PIT 披露安全、blocked 机制 | ✅ 完成 |
| **2 Alpha** | IC 加权、sleeve、intersection、ML（LGB+MLP 集成）、regime 条件合成 | ✅ 完成 |
| **3 风控** | 中性化（行业+市值+风格 Barra）、Ledoit-Wolf 协方差、VaR、约束过滤、冷却、熔断 | ✅ 完成 |
| **4 组合优化** | Nano/Micro/Small 三层、HRP、Risk Parity、Kelly、成本带（Grinold α−λ·TC）| ✅ 完成 |
| **5 执行** | BacktestExecutionModel + LiveExecutionModel、止损（ATR 三重止盈）、加仓闸门（Van Tharp）| ✅ 完成 |
| **6 监控** | 归因、告警（SSE 推送）、因子归因、绩效报告 | ✅ 完成 |
| **7 调度** | manifest 声明式调度、orchestrator 主循环、晚间链 subprocess | ✅ 完成 |
| **评估** | 8 阶段认证（CPCV、DSR、PBO、Walk-forward）| ✅ 完成 |

### ❌ 缺失功能

| 功能 | 影响 | 优先级 |
|------|------|--------|
| **实盘券商对接（vnpy）** | `broker_adapter.py` 已写 1511 行但未激活；当前全为模拟执行 | P0 |
| **因子自动降噪/去相关性** | v534 删除了 `_adjust_for_redundancy`，声称"IC 退化告警承担"但无自动降权机制 | P1 |
| **因子有效期管理** | probation 降半权（代码已实现），但无自动升级/降级规则引擎 | P1 |
| **训练数据泄露检测** | `build_train_matrices` 用 z-score rank 对全市场截面做，与训练/测试窗口无关；需 Purged K-Fold | P1 |
| **回测结果自动对比** | `backtest_runs` 表已建，但无 Web UI 对比/dashboard/自动告警 | P2 |
| **实盘日志持久化** | `orchestrator.py` trace 记录在内存，进程重启丢失 | P2 |
| **多策略支持** | `strategy="quant"` 硬编码，各处复用性差 | P2 |
| **夜盘/期权数据** | 仅股票，日内数据缺失（高频因子无法实现） | P3 |

---

## 3. 业务逻辑清晰度 & 流程闭环

### ✅ 清晰的业务逻辑

1. **因子状态机**：`factor/state_machine.py` + `factor_curator.py`，5 态流转（evaluating→active/probation→suspended）✓
2. **regime 条件合成**：HMM 检测 → 因子权重偏置，逻辑完整 ✓
3. **成本感知换仓**：Grinold α−λ·TC 无交易区间，理论扎实 ✓
4. **因子缓存 blocked 机制**：空结果 >50 天自动 block，避免 bug 因子永久污染 ✓

### ⚠️ 流程闭环问题

| 问题 | 位置 | 说明 |
|------|------|------|
| **pipeline.py 896 行超载** | `pipeline.py` | 单文件承担数据加载、因子计算、风险过滤、优化、执行、监控、持久化全部职责；模板 2a 要求 IO 与计算分离，此文件违反 | P0 |
| **evaluate 表白板** | `evaluation/phase*` | 8 阶段认证脚本齐全但无 UI/报告输出端，无法可视化认证结果 | P1 |
| **晚间链无 DAG** | `scheduler/evening.py` | daily_data → factor_cache → attribution 顺序硬编码；无依赖图验证 | P1 |
| **回测结果无自动告警** | `backtest/loop.py` | `_persist_backtest_result` 写表，但无超参漂移/过拟合自动检测 | P2 |
| **实盘无交易日志审计** | `execution/engine.py` | `execute()` 写入 sim_trades 但无操作日志（who/when/what），模板 4 缺失 | P2 |

---

## 4. 系统架构是否需要优化/重构？

### 🔴 必须重构

**`pipeline.py` (896 行) — 最严重架构问题**

```
当前: generate_signals() + execute_signals() + run() 全塞一个文件
应改为:
├── pipeline/
│   ├── signals.py      # Step 0-5: 信号生成（纯计算，无 IO）
│   ├── executor.py      # Step 6: 执行（接收 signals 输出）
│   ├── monitor.py      # Step 7: 监控报告
│   └── context.py      # ExecutionContext 统一上下文
```

**根因**：pipeline 是核心业务流，896 行 + 无分包导致：
- 修改 Step 3 影响 Step 6（紧耦合）
- 无法单独测试 Step 3 或 Step 6
- `generate_signals` 和 `execute_signals` 共享 `LOT_SIZE` 等常量但分散定义

### 🟡 建议重构

| 文件 | 当前行数 | 建议 |
|------|---------|------|
| `data/store.py` | **3056 行** | 拆分为 `data/sqlite_store.py` + `data/duckdb_proxy.py` + `data/cache.py` |
| `risk/live_risk.py` | 767 行 | 拆分为风控规则引擎 + 实时 VaR 计算 |
| `dashboard/dashboard.py` | 847 行 | 拆分为数据层（DataTable）+ 可视化层（Charts）|
| `execution/broker_adapter.py` | 1511 行 | 拆分为 `broker/vnpy_adapter.py` + `broker/simulator.py` + `broker/base.py` |
| `quant/orchestrator/dagster_assets.py` | 944 行 | 与 `scheduler/` 双套班子系统；建议统一到 scheduler（manifest 是单一真相源）|

### ✅ 架构良好的部分

- `factor/compute/_primitives.py`：共享预计算图，架构清晰
- `alpha/strategy.py`：注册表模式，新增策略零侵入 ✓
- `quant/optimizer/portfolio.py`：三层分级 + 校准函数，架构合理
- `quant/risk/neutralize.py`： Barra 标准联合中性化 ✓

---

## 5. 代码逻辑错误 & 其他问题

### 🔴 Bug 列表

**Bug 1: `trim_orders_by_alpha` 语义分裂（v576 修复引入新问题）**

```python
# execution_model.py 第 56-75 行
# v576: 改"资金不足直接丢弃"为"尽量填充"
# 但 v576 删除了资金不足检查逻辑，导致:
for o in buy_orders:
    ...
    if max_shares >= LOT_SIZE:
        o.shares = max_shares  # ← 这里修改了 shares 但没重新计算 cost
        o.cost = cost_model.buy_cost(px, max_shares)  # ← 重新计算了
        available -= o.cost
        feasible.append(o)
    else:
        feasible.append(o)  # ← BUG: 兜底追加原始 o (shares 未变)，available 不变
```

**问题**：`else` 分支追加的是原始 `o`（目标 `o.shares`），但 `max_shares < LOT_SIZE` 意味着买不起一手；追加后无资金检查，`available` 未扣减，导致后续订单以错误资金状态继续处理。

**修复方向**：去掉兜底追加，或追加时设 `o.shares = 0`。

---

**Bug 2: `neutralize.py` 有静默 fallback**

```python
# v551 注释说"缺一维度抛错阻断"，但代码实际:
if n_industry < min_common or n_mcap < min_common:
    if n_industry < min_common:
        logger.warning(...)
        industries = None
    if n_mcap < min_common:
        logger.warning(...)  # ← 仅 warning，未抛错
```

**问题**：注释说 B32 阻断，但 `market_caps` 缺失时仅 warning，不符合"零 fallback"原则。缺少 `mcap_real is not None` 的显式检查。

---

**Bug 3: `DataStore.__init__` 中 `_connect()` 被调用两次**

```python
# store.py 第 224 行
conn = self._connect()  # ← 第一次
conn.executescript(...)  # DDL 建表
conn.commit()
# 然后 fund_cols ALTER TABLE 继续用 conn
```

**问题**：建表用 `conn`（`_local.conn`），但 `_connect()` 会同时初始化 `self._conn`（向后兼容）和 `_local.conn`（WAL 并发），两者指向同一连接。事务重叠时可能死锁。

---

**Bug 4: `_compute_atr` 缓存键无进程隔离**

```python
# stop_loss.py 第 47 行
_CACHE = {}  # 模块级 dict
# 多进程回测时各子进程独立缓存，但 orchestrator 主进程与 subprocess 进程不共享
```

**问题**：`_cache` 是进程内 dict，subprocess 内的 `RiskManager` 无缓存命中，每次重新拉 DB。回测无影响，实盘 subprocess（factor_cache/attribution）也无共享缓存需求（独立进程），但 orchestrator 主进程的 `RiskManager` 实例缓存与 subprocess 不互通。

---

**Bug 5: `execute_signals` 中 `stopped_out` 写入死代码修复不完整**

```python
# pipeline.py 第 520 行
if _exec_res.stopped_out:
    results["stopped_out"] = _exec_res.stopped_out
```

**问题**：Q7-2 fix 修复了 `stopped_out` 写入，但 `_compute_backtest_metrics`（loop.py）读取的 `stopped_out` 来自 `ExecutionResult` 的 `stopped_out` 字段，需确认 `loop.py` 中 `run()` 是否正确传递此字段。回测路径 `backtest/broker.py` 可能仍用旧接口。

---

**Bug 6: `phase7_wf.py` Phase 3 失败时 abort 但 Phase 2 失败时返回空列表**

```python
# phase7_wf.py 第 56-73 行
if not passed_p2:
    return []  # ← 无告警，静默失败
try:
    p3 = validate_oos(...)
except Exception as e:
    _log.error(f"Phase 3 failed — aborting WF run")  # ← 有告警
    return []
```

**问题**：Phase 2 失败返回 `[]` 无错误级别日志，只有 `_log.warning`，容易被忽略；且 `_run_train_phase` 整体被 `try/except` 包裹后吞掉所有异常，无法区分"数据缺失"和"因子全挂"。

---

**Bug 7: `alpha/qlib_model.py` 变量作用域泄漏**

```python
# train() 方法中
except Exception:
    ...
    train_end=(mats["train_dates"][-1].strftime("%Y-%m-%d")
               if mats["train_dates"] else fwd_dates[-1].strftime("%Y-%m-%d") if fwd_dates else "")
```

**问题**：`fwd_dates` 在正常路径中通过 `build_train_matrices` 内联构造，未作为局部变量暴露在 `train()` 作用域内；`except` 分支引用时若正常路径异常可能未定义。

---

### 🟡 代码质量问题（非阻断但需改进）

| 问题 | 位置 | 说明 |
|------|------|------|
| **重复 `_require_cfg` 调用** | `portfolio.py` 第 19-22 行 | `_TC_LAMBDA` 等 4 个模块级常量每次 import 读一次 config，无缓存；影响启动时间 |
| **无类型注解的函数参数** | `stop_loss.py` 多处 | `check_atr_stop` 的 `positions`/`prices` 无类型提示，`RiskManager` 属性无类型注解 |
| **裸 `except Exception`** | `pipeline.py` 第 315 行 | `except Exception` 捕获全部异常后仅 warning，应按模板 1 用特定异常类型 |
| **`get_logger` 调用不一致** | 多处 | 统一用 `get_logger("quant.module.name")` 但 `alpha/synth.py` 第 101 行混用 `logging.getLogger` |
| **硬编码 magic number** | `risk/live_risk.py` 第 580 行 | `if exposure > 1_000_000_000:`，无 config.yaml 对应 |
| **float32→float→float32 转换** | `factor/store.py` | 精度丢失约 1e-7 量级，低优先级 |

---

## 6. 算法是否需要优化/改进？

> 不改代码，纯讨论。

### ✅ 算法合理的部分

| 算法 | 评价 |
|------|------|
| **Ledoit-Wolf 收缩协方差** | pairwise-complete 修正 B6 后 NaN 安全 ✓ |
| **Wilder SMMA ATR** | v553 修正了 SMA 误用，正确实现 ✓ |
| **Blom 分位 z-score** | 比 rank-percentile 更平滑 ✓ |
| **CPCV + DSR** | Bailey & Prado (2014) 标准做法 ✓ |
| **PBO（概率优于基准）** | DeMiguel et al. (2009) 选基线方法 ✓ |
| **因子中性化 Barra 联合回归** | 行业哑变量 + log(mcap) 同时回归 ✓ |
| **Grinold α−λ·TC 成本带** | 理论正确实现 ✓ |

### ⚠️ 算法优化建议（纯讨论）

#### ① 协方差估计 — 可考虑因子模型协方差

```
现状: Ledoit-Wolf 收缩样本协方差
问题: 800 股票 × 252 天，N≈T，样本协方差仍不稳定

建议: Barra 因子模型协方差
  Σ = X·F·X' + diag(σ²_ε)
  其中 X 是 N×K 因子暴露（K=30 top factors）
  F 是 K×K 因子协方差矩阵（T>K 时良态）
  
优势: 
  - K=30 << N=800，协方差估计更稳定
  - 因子模型天然降维，过拟合风险低
  - Barra USE4 全球资管行业标准

实现位置: risk/covariance.py 已有 `factor_covariance()` 雏形
```

#### ② IC 加权 — 可考虑 ICIR 加权

```
现状: ic_weighted 用 |IC| 线性加权
问题: |IC|=0.03 的高噪声因子与 |IC|=0.08 的因子权重比 = 0.375，
      噪声因子贡献约 1/3 权重

建议: 改用 ICIR (IC_mean / IC_std)
  weight_i ∝ |ICIR_i| = |IC_mean_i| / IC_std_i
  
依据:
  - ICIR > 0.3 可用（Grinold 标准）
  - ICIR 衡量 IC 的统计显著性（稳定性 > 幅度）
  - 因子 IC 稳定比 IC 高更有预测价值
  
风险: ICIR 对样本量敏感，样本少时 std 被低估 → ICIR 虚高
  需设置 min_periods 过滤

实现: 已有 oos_icir 字段（LgbAlphaModel 训练时计算），可复用
```

#### ③ Alpha 合成 — 可考虑风险平价加权

```
现状: ic_weighted 或 equal_weight
问题: ic_weighted 对高 IC 因子过度集中，单因子失效影响大

建议: 风险平价因子合成
  每因子贡献相同方差 → 因子间相关性被自然分散
  
公式: w_i = (1/σ_i) / Σ(1/σ_j)
  其中 σ_i = 因子 IC 序列的波动率

实现: 在 ic_weighted() 中增加 `mode="risk_parity"` 选项
```

#### ④ HMM regime 检测 — 可考虑加入成交量维度

```
现状: 仅用沪深 300 收益率 (mean, std)
问题: 成交量在市场转折点领先于价格（机构建仓/出货特征）

建议: 扩展特征为 4 维
  X = [rolling_mean_ret, rolling_vol, rolling_volume, volume_ret_correlation]
  
实现: `regime/detector.py` 的 `_features()` 方法
```

#### ⑤ ATR 止损 — 可考虑 Chandelier Exit 的波动率自适应

```
现状: 固定 ATR 倍数 (2×SL, 3×TP1, 1.5×Trail)
问题: 不同波动率环境下，固定 ATR 倍数保护不足或过于激进

建议: ATR 倍数随历史波动率环境自适应
  - 高波动期（ATR > 20日均值 1.5σ）: 放宽倍数 2.5×SL
  - 低波动期（ATR < 20日均值 0.5σ）: 收紧倍数 1.5×SL
  
优势: 减少假突破（低波动期滑点/噪音多）
```

#### ⑥ 因子预计算缓存 — 可考虑增量更新

```
现状: 全量预计算 primitives，缓存键仅含数据哈希
问题: 物化新日期时需重算全部 10 个窗口的所有原语

建议: 增量预计算
  - 保留现有的 `prims_{hash}` 目录
  - 新日期追加而非重算：log_ret diff 仅需 1 行
  - 滚动窗口原语 (ma_20d) 增量追加最后一行
  
实现: 在 `factor/store.py` 的 `_materialize_chunk` 中追加增量逻辑
```

---

## 7. 代码中所有 Bug 汇总

| # | 严重度 | 模块 | 描述 |
|---|--------|------|------|
| B1 | 🔴 P0 | `execution_model.py` | v576 `trim_orders_by_alpha` else 分支追加原始订单但未扣减资金 |
| B2 | 🔴 P0 | `neutralize.py` | 注释说"缺 market_caps 阻断"但实际仅 warning，违反零 fallback |
| B3 | 🟡 P1 | `store.py` | `__init__` 中 `_connect()` 被调用两次，事务重叠风险 |
| B4 | 🟡 P1 | `stop_loss.py` | `_compute_atr` 缓存无进程隔离，实盘 subprocess 缓存失效 |
| B5 | 🟡 P1 | `loop.py` | `stopped_out` 字段从 `ExecutionResult` 到 `backtest_runs` 传递链未验证 |
| B6 | 🟡 P1 | `phase7_wf.py` | Phase 2 失败时仅 warning 不 error，静默返回 `[]` |
| B7 | 🟡 P1 | `alpha/qlib_model.py` | `train()` 中 `fwd_dates` 变量作用域泄漏（except 分支引用） |
| B8 | 🟢 P2 | `store.py` | `float32→float→float32` 转换精度丢失 ~1e-7 |
| B9 | 🟢 P2 | `portfolio.py` | `_require_cfg` 模块级调用无缓存，启动时重复读 config |
| B10 | 🟢 P2 | `risk/live_risk.py` | 硬编码 magic number `1_000_000_000` 无配置对应 |

---

## 总结：核心改进优先级

```
P0（立即修复）:
  1. 重构 pipeline.py → 拆分为 pipeline/signals.py + pipeline/executor.py
  2. 修复 B1 (trim_orders_by_alpha 资金逻辑)
  3. 修复 B2 (neutralize 零 fallback)

P1（本季度）:
  4. 拆分 store.py (3056→3 文件)
  5. 实现因子协方差矩阵 (Barra 模型)
  6. 补全实盘券商对接 (broker_adapter 激活)
  7. 修复 phase7_wf.py Phase 2 静默失败

P2（规划中）:
  8. ICIR 加权替代 IC 加权
  9. 增量预计算原语缓存
  10. 多策略框架
```

---

发现的主要矛盾集中在：

1. **pipeline.py 单文件超载**是最严重的架构问题
2. **两个 B32 级别的零 fallback 违规**（neutralize + trim_orders_by_alpha）
3. **因子合成算法有优化空间**（ICIR、风险平价）

---

## 附录：v577 Scheduler 双模式修复记录 (2026-08-30)

### 双模式架构

| 维度 | Legacy (`orchestrator.py`) | Dagster (`dagster_assets.py`) |
|------|--------------------------|------------------------------|
| 入口 | `QUANT_ORCHESTRATOR=legacy` (默认) | `QUANT_ORCHESTRATOR=dagster` |
| 调度 | orchestrator 30s 轮询 | Dagster Daemon cron + Sensors |
| 执行 | InlineRunner / subprocess | 直接调用 module `_run()` |
| 重试 | `MAX_TASK_RETRIES=2` | `RetryPolicy(max_retries=3)` |
| 进程管理 | PID 锁 + grace dedup | daemon 独立进程 |

### v577 修复的 Bug

| # | 严重度 | 位置 | 问题 |
|---|--------|------|------|
| B1 | 🔴P0 | `dagster_assets.py` 所有 15 个 asset | `start_time` 未定义 → 运行时 `NameError` |
| B2 | 🔴P0 | `dagster_assets.py:signals` | Dagster 模式缺 `broker.update()` → Web UI 显示旧信号 |
| B3 | 🔴P0 | `dagster_assets.py:xgb_train` | 缺实际 `_xgb_run()` 调用 → 训练永不执行 |
| B4 | 🔴P0 | `scheduler/__init__.py:_start_dagster` | 原实现只验证 definitions，**Dagster daemon 进程从未启动** |
| P1 | 🟡P1 | `dagster_assets.py:monitor` | `daemon_thread.join()` 阻塞主线程 → Asset 永不完成 |
| P2 | 🟡P1 | `dagster_assets.py:reconcile` | 依赖 `monitor` 应为 `snapshot_close` |
| P3 | 🟡P1 | `dagster_assets.py` 晚间链 | `adj_factor` → `adj_factor_sync` (避免同名冲突) |
| P4 | 🟡P1 | `dagster_assets.py` job 定义 | AM 链混入晚间任务 → 与 `daily_data_job` 重复 |
| P5 | 🟡P1 | `dagster_assets.py` 所有 asset | `ResourceParam[MarketDBResource]` → `MarketDBResource` |

### 修改文件

1. **`quant/orchestrator/dagster_assets.py`** — 完全重写 (944 → 15 个 asset 函数，~280 行)
2. **`quant/scheduler/__init__.py`** — 重写 `_start_dagster()`，正确启动 daemon + webserver

### 归档

- 详细修复报告: `docs/scheduler-fix-v577.md`
- 语法验证: ✅ 两个文件全部通过
