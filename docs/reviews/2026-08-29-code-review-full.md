# quant 项目代码审查报告（代码层，不含文档）

- 日期：2026-08-29
- 范围：`quant/` 全部 234 个 Python 文件（约 6.8 万行），不含 docs/、CLAUDE.md、HANDOFF.md、README 等任何 markdown
- 方法：5 路并行 explore agent 分模块读源码 + 综合；所有结论附 `file:line`
- 目的：北极星目标（¥5000 → ¥1M）进展评估；技术/功能/业务闭环/架构/逻辑错误/算法/bug 七问
- **状态更新（2026-08-29, v568）**：🔴 C1（财务因子 look-ahead，`stat_date` 而非 `pub_date`）已按方案 A 修复。
  新增 `quant/factor/compute/_pit.py`（PIT 披露口径，与 `store.get_financials` 一致）；修正 `_preload.py` 两处切片 +
  `fundamental.py`/`missing.py` 全部财务因子 SQL/aux 取数路径。因子 compute 测试 16 + aux/synth/marginal 17 全绿。
  **后续待办**：需按 PIT 重算 factor_cache + IC 表，确认哪些财务因子仍有效（IC 预期下降）。C2/C3/C4 等未动。

---

## 总评

系统核心投研闭环（数据→因子→Alpha→风控→优化→回测→执行→监控）在架构层面健全、可运行；PIT/IC 权重/T+1/涨跌停/除权跳过的处理大多正确。但存在 3 类系统性致命问题，直接侵蚀 alpha 真实性：

1. **前视偏差（lookahead）**：财务因子用 `stat_date` 而非 `pub_date`，IC 被系统性高估（最高优先级）。
2. **ML alpha 完全未接入信号路径**：训练好却不参与实盘/回测选股，`lgb/xgb` 模式还会崩溃。
3. **约 7000–8000 行"平台"代码严重违反"无 alpha 贡献不建设"原则**，且已产生具体 bug。

---

## Q1. 技术选型是否合适？缺什么？

**合适：**
- SQLite(写) + DuckDB(分析镜像) + gzip/parquet 因子缓存 + fcntl IPC 限流：对单机 ¥5k 起步量级是正确轻量选型。
- LightGBM / Ledoit-Wolf 收缩 / HRP / Almgren-Chriss：工具选得对（实现正确性见 Q6）。

**必须补（直接服务目标）：**
- 真实交易成本进入优化目标（当前仅事后成本带抑制，初始权重忽略成本/换手）。
- 流动性约束（仓位 ≤ x% 20日ADV）；`constraints` 只做候选集预筛。
- 因子 PIT 复权校验/对账（见 Q7-C3）。
- 真正的 CPCV（组合 Purged CV），当前是顺序 Purged WF（Q7-M4）。

**应删除而非新增（去伪）：** tenant 多租户 DRF 调度、Dagster 双调度器、Prometheus/Grafana/Tracing exporter、compliance/canary、stress_test/live_stress、emergency/drills、dashboard.py —— 全部零引用、对单账户无 alpha 价值。

---

## Q2. 已实现 / 未实现

**已实现（核心，可用）：**
- 因子引擎：~119 个注册因子（价量 ~60% + 基本面 + 事件/另类），primitives→dispatch→price/fundamental 分层清晰，FACTOR_SHORTCUT 记忆化。
- Alpha：sleeve / ic_weighted / equal_weight / intersection + regime 加权；LGB/XGB/Ensemble 模型类（但未接信号，见 Q7-C2）。
- 风控：Ledoit-Wolf 协方差、行业/市值中性化、VaR/CVaR、约束过滤。
- 优化：Nano/Micro/Small 三层（HRP/Kelly/均值方差）、成本带、迭代裁剪。
- 回测：walk-forward 事件驱动、PIT IC 重训、T+1/涨跌停/停牌/除权建模、DSR/PBO/CPCV 评估链。
- 执行：Backtest/Live 双模型、ATR/硬/追踪止损、冷静期、成本模型。
- 调度：manifest 声明式 + 30s 轮询编排器 + 晚间链（data→factor→attribution→lgb_train），闭环可跑。
- 监控/Web：Flask + SSE 仪表盘、指标持久化、告警规则。

**未实现 / 虚设（关键缺口）：**
- ML alpha 实际选股（已训练但不调用）→ Q7-C2。
- Almgren-Chriss 冲击真正接入回测（存在但断开，回测用线性+固定滑点）→ Q7-M1。
- 日内/部分成交、ADV 参与率冲击（仅日频开盘成交）。
- compliance/canary/多租户/stress/emergency 全是死代码，不算功能。

---

## Q3. 业务逻辑是否清晰？流程闭环？

**核心循环已闭环且清晰**：`orchestrator → evening链(daily_data→adj_factor→duckdb_sync→factor_cache→attribution→lgb_train) → 次日 signals(generate_signals) → execute → monitor(止损) → reconcile`。依赖门控、重试预算、僵尸 PID 自愈均到位。

**三处逻辑断裂/降级：**
- ≥¥100k 资金档（¥5k→¥1M 主战场）用 HRP，**alpha 量级在仓位定价时被丢弃**，只用于选股（`quant/optimizer/portfolio.py:335-354, 842-910`）。
- 双重中性化 + 行业覆盖薄时**硬崩溃**而非降级（`pipeline.py:445,522-524` + `neutralize.py:206-211`）。
- `manifest.py:145-154` 的 `duckdb_sync` 作为独立 TaskSpec **从未被编排器分发**，只作 `evening._CHAIN` 内部阶段 → 双真相源。

结论：选股逻辑清晰且闭环；但"alpha 如何转化为仓位权重"在资金增长档存在设计断点，且若干降级路径会直接崩而非优雅退化。

---

## Q4. 架构是否需优化/重构？

**需要，方向是"减法 + 修两处重复"，不是加层：**

1. **删除 ~7000-8000 行死代码**（tenant/monitoring exporter+dagster+grafana+tracing/alerting、orchestrator/dagster_assets、dashboard、compliance、canary、stress_test/live_stress、emergency/drills）—— 违反北极星准则，且已产出真实 bug（`runners.py:223`、`prometheus_exporter.py:325-458`）。
2. **收敛三处"双真相源"**：universe 3 套（`universe_repo`/`store`/`duckdb_store`，Q7-M6）、数据源抽象 `data/sources/` 与 `store.update_daily` 内联 fetcher 并存（Q7-M5）、factor 物化引擎 subprocess(store.py) vs Ray(distributed/engine.py) 并存（Q7-M10）。
3. **`broker_adapter.py` 类层级定义三遍**（~1-449/450-994/995-1510），仅末份生效 → 最严重代码健康隐患（Q7-C4）。
4. **`live_engine.py`（TWAP/VWAP/iceberg/POV 切片，807 行）是孤儿**：实际实时路径走 `LiveExecutionModel.execute_buys`，高级切片引擎未接入 → 接或删。

架构分层本身（factor/alpha/risk/optimizer/execution/backtest 单向依赖）干净，无需推倒重来。

---

## Q5. 代码逻辑错误/其他问题？是否需重构？

**需重构，聚焦：删死代码、收敛重复实现、修前视偏差与 ML 接线、修 broker_adapter 三重复写。** 关键 bug 见 Q7。

其他显著问题：
- `monitor/metrics.py:59-70` 指标 `persist()` **无界 append-only**，无 upsert/裁剪。
- `orchestrator.py:219` 周末 `daily_repair` **阻塞主循环最多 3h**，v556 非阻塞修复只做一半。
- `monitor/attribution.py:88` 因子暴露回归**无截距列**（强制过原点），β 偏估。
- `web/app.py` `api_scheduler` cron 覆盖逻辑全死（守护进程非 crontab）。
- `trade_repo.get_open_position_cost` 用 `NOT IN (sold symbols)`，**部分平仓后重开仓成本基数为 0**（Q7-M7）。

---

## Q6. 算法是否需要优化改进？（讨论，不改代码）

**已验证正确（无需动）：** Ledoit-Wolf 收缩（`covariance.py:88-156`）、HRP 递归二分（`hrp.py:43-126`）、`_iterative_clip` 收敛、ICIR→权重保留符号、回测 PIT/IC 重训无前视、T+1 逻辑、FIFO 成本。

**需优化/隐患：**
- 财务因子前视（最高 ROI）：改用 `store.get_financials`（已 PIT 正确）替代所有 `stat_date<=` 重查。
- Kelly 是单资产启发式，非真 Kelly：`kelly.py:84-123` 各资产独立 `f=μ/σ²`，忽略跨资产协方差；μ 是 IC 缩放而非收益预测。安全但被误标，规模放大后相关性簇超配。
- HRP 默认档丢弃 alpha 量级（Q3）—— 应显式决定 ≥¥100k 是否用 alpha（切 mean_variance 或 alpha-tilt 协方差）。
- ML 特征合同不一致：LGB 先 rank 后 reindex，XGB 先 reindex 后 rank（候选子集 vs 全市场漂移，`xgb_model.py:264-269`）；Ensemble/rolling CV 用 raw 因子训练，与 LGB 口径不同。
- DSR 用硬编码偏度/峰度（`loop.py:169` 写死 -0.5/8.0），与因子级 DSR 互不一致；Sharpe SE 的 ddof 不一致（Q7-M3）。
- HMM 状态标签跨重训可能翻转（`detector.py:71-77`），regime 加权会 boost 错因子集（Q7-M11）。
- CPCV 实为顺序 Purged WF，仅 train-end purge（Q7-M4）。
- 回测归因锚点错误：用 `next_close/today_close` 而非 `next_open`（m1），隔夜跳空误归因给因子。

---

## Q7. 所有 Bug 汇总（按优先级）

### 🔴 Critical（直接污染 alpha / 崩溃）
| # | Bug | 位置 |
|---|-----|------|
| C1 | 财务因子用 `stat_date` 而非 `pub_date` → 1-4 月前视，~15 因子 IC 虚高 | `fundamental.py:839,862,694,233,285,383,1129`；`missing.py:141,201,289,296,302` |
| C2 | `combine_mode="lgb"/"xgb"` 崩溃（ML 模型无 `combine` 方法）；且 ML alpha 从不参与信号路径 | `alpha/model.py:55-71`,`strategy.py:262-280`,`pipeline.py:468-475` |
| C3 | DuckDB 价格镜像不做复权对账 → 除权后动量/波动因子在 DuckDB 上用旧价（全管线消费路径） | `store.py:2665`,`duckdb_store.py:620-653`,`store._rebase_ex_dividend` |
| C4 | `broker_adapter.py` 类层级定义三遍，仅末份生效，前两份编辑被静默遮蔽 | `broker_adapter.py` 1-449/450-994/995-1510 |

### 🟠 Major
| # | Bug | 位置 |
|---|-----|------|
| M1 | Almgren-Chriss 冲击未接入回测（无 `daily_volume`），回测走线性+固定滑点 | `execution_model.py:376-401`,`cost.py:94-124`,`engine.py:229` |
| M2 | 价格让步双重计费：impact 膨胀价 + 固定 0.1% 滑点；`o.cost` 更新为死代码 | `execution_model.py:397`,`cost.py:99,105` |
| M3 | DSR 硬编码偏度/峰度 + ddof 不一致（与因子级 DSR 冲突） | `loop.py:169`,`deflated_sharpe.py:175` |
| M4 | "CPCV" 实为顺序 Purged WF，fold 数少 1、仅单侧 purge；PBO 力度被高估 | `cpcv.py:38-87`,`pbo.py:53` |
| M5 | `data/sources/` 与 `store.update_daily` 内联 fetcher 并存，circuit-breaker 在生产路径不生效 | `data/sources/*`,`store.py` |
| M6 | `get_fundamentals` 用全局 MAX(date) 而非按符号 → 非全局最新日重估的符号估值 NaN | `store.py:2939` |
| M7 | `get_open_position_cost` 用 `NOT IN (sold)` → 重开仓成本基数为 0 | `trade_repo.py` |
| M8 | 新上市股 `batch_start` 发 compact 日期 → pytdx 静默跳过全部行 | `store.py:2528-2530` |
| M9 | 双重中性化 + 行业覆盖薄时硬崩溃 | `pipeline.py:445,522-524`,`neutralize.py:206-211` |
| M10 | `DistributedFactorEngine` 在 Ray 内嵌套 subprocess → 资源爆裂/坏；引用旧 SQLite 路径 | `distributed/engine.py:304` vs `store.py:661` |
| M11 | HMM 状态标签跨重训可能翻转 → regime boost 错因子 | `detector.py:71-77` |
| M12 | `runners.py:223-225` `_dispatch` subprocess 分支用 `import_module` 加载语句串 → ImportError（潜伏） | `runners.py:223` |
| M13 | 周末 `daily_repair` 阻塞主循环 ≤3h，v556 修复半途而废 | `orchestrator.py:219` |
| M14 | 编排器周末冻结告警/SSE/weekly_eval | 同上 |
| M15 | 状态机 `register_factor` 绕过 `transition()`；FSM 表死代码；`evaluate_factor` 与 `compile_evaluate_register` 重复且签名错 | `state_machine.py:283,217,330` |
| M16 | 常量截面因子（macro×4, epa）静默死亡却计入"100+" | `fundamental.py:1292-1326,1191` |
| M17 | 盈利衰减只用 income 表 `break` 定基 → 非 income 因子 decay 基错 | `dispatch.py:231` |
| M18 | ~7000-8000 行死代码（tenant/monitoring exporter+dagster+grafana+tracing/alerting/dashboard/compliance/canary/stress/emergency/dagster_assets） | 各模块 |
| M19 | 因子暴露回归无截距项 → β 偏估 | `attribution.py:88` |
| M20 | `metrics.persist()` 无界 append | `monitor/metrics.py:59-70` |

### 🟡 Minor（精选）
- `expr_compiler.ts_rank` 在 `_TS_FNS` 但 `evaluate` 未处理 → 静默 NaN（`expr_compiler.py:225,186`）
- XGB 推理 z-score 在候选子集而非全市场（`xgb_model.py:264-269`）
- `compute_ic` 丢弃最新 `lookback//2` 个 IC 观测（`ic.py:157`）
- `residual_momentum` raw 路径 000300 缺失时 KeyError（`_momentum.py:64`）
- `IncrementalCovariance` 实为 O(N³) 每轮全量，非增量（`covariance.py:357-367`）
- `data_cache` 因 mtime 几乎每次回测失效（`data_cache.py:93-95`）
- 停牌股回测按成本 MTM、实时却阻断卖出 → 不一致
- `manifest.duckdb_sync` 独立 TaskSpec 从未分发（双真相源）
- `web/app.py` cron 覆盖逻辑全死
- `alerts.py:54-60` 日期类型脆弱
- `NoopBackend` 缓存非线程安全且漏存 falsy 值（`cache.py:41-80`）
- `core/version.py` `__version__` 与 vNNN 体系脱节
- `prometheus_exporter.py:325-458` RemoteWriteClient 三重复写、末份静默推空

---

## 下一步优先级（讨论，未改代码）

1. **先修 C1（财务前视）**——alpha 真实性根基，影响 ~15 因子 IC/IR 结论。
2. **修 C2（接 ML alpha 或禁用 lgb/xgb 搜索）**——一半"模型 stack"目前是装饰。
3. **修 C3/C4 + 删死代码（M18）**——C4 最大代码健康雷，M18 最大反原则负债。
4. **决策 ≥¥100k 档是否用 alpha 定价（M1/HRP）**——直接决定 ¥5k→¥1M 路径仓位效率。
5. **成本/流动性 realism（M1/M2 + ADV 约束）**——规模放大后的 P&L 误差主源。
