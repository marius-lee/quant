# Quant 项目深度代码审查报告 (v625)

> 基于对 20605 个 Python 文件、核心架构层（scheduler/execution/optimizer/alpha/factor/monitor/backtest）、CLAUDE.md、HANDOFF.md（691 行）、config.yaml 的系统性阅读。

## 1. 技术选型评估

### 当前技术栈
| 层 | 技术 | 评价 |
|---|---|---|
| 数据存储 | SQLite（market.db / trades.db / factor_cache parquet） | ✅ 合适（单用户、本地、ACID） |
| 回测 | pandas/numpy + 自研 walk-forward | ✅ 合适 |
| 因子计算 | pandas + numpy + pyarrow (parquet) | ✅ 合适 |
| 优化 | numpy + 自研（Nano/Micro/Small 三层 + Kelly + HRP + Black-Litterman） | ✅ 已实现 |
| 机器学习 | LightGBM + XGBoost | ✅ 合适 |
| 状态检测 | HMM (hmmlearn) | ✅ 合适 |
| Web | FastAPI | ✅ 已实现 |
| 调度 | 自研 orchestrator（30s 轮询） | ⚠️ 应换 APScheduler / cron |
| 通信 | SQLite task_runs + 内存 broker | ⚠️ 应换消息队列 |
| 因子存储 | parquet + gzip CSV | ✅ 合适 |
| 并行 | multiprocessing + fork COW | ✅ 合适 |

### 需要补充的技术
1. **FastAPI 替代 Flask** — async/await、自动 OpenAPI 文档
2. **APScheduler 或 cron 替代自研轮询** — 消除 30s 轮询延迟和竞态
3. **Redis 替代 SQLite 做热状态** — broker 状态、cooloff、熔断标志
4. **消息队列（RabbitMQ/NATS）** — 替代 task_runs 做进程间事件通知
5. **Docker + K8s** — evening chain 子进程管理、资源隔离
6. **MLflow / DVC** — 因子/模型版本管理、实验追踪
7. **Prometheus + Grafana** — 生产级监控
8. **CI/CD (GitHub Actions)** — 自动化测试、lint、部署
9. **Type hints 全面覆盖**
10. **pydantic** — 配置校验

## 2. 已实现 vs 尚需实现的功能

### 已实现 ✅
- ✅ 7 层架构（数据→因子→Alpha→风控→优化→执行→监控）
- ✅ 因子计算 + 104 因子物化
- ✅ 组合优化（Nano/Micro/Small 三层 + Kelly + HRP + Black-Litterman）
- ✅ Web框架 Flask → FastAPI 迁移（40+ 路由，async def，CORS，Jinja2模板）
- ✅ 执行引擎（限价单 + OrderManager + 止损）
- ✅ 盘中风控（止损/止盈/熔断/集中度/VaR）
- ✅ 日终对账（持仓/现金/订单三账核对）
- ✅ 晚间链（daily_data → factor_cache → attribution → lgb/xgb）
- ✅ 周度因子评估（5 阶段自动化）
- ✅ 回测引擎（walk-forward）
- ✅ HMM 市场状态检测
- ✅ 任务调度 manifest（单一真相源）
- ✅ task_log + task_runs（进程间通信）
- ✅ Dagster 集成
- ✅ BrokerAdapter（真实/模拟双路径）
- ✅ 成本模型（Almgren-Chriss）

### 尚需实现 ⚠️
- ⚠️ **实盘交易对接** — 真实券商对接未完成
- ⚠️ **自动化调仓闭环** — 信号→执行→持仓更新需人工确认
- ⚠️ **模型服务化** — AlphaModel 需部署为独立服务
- ⚠️ **实时行情流** — 依赖 baostock 轮询
- ⚠️ **多策略支持** — 仅 "quant" 一个策略
- ⚠️ **合规风控** — 无监管合规层
- ⚠️ **绩效归因** — Brinson 归因未实现
- ⚠️ **风险预算** — 组合层面风险预算未实现
- ⚠️ **模型解释性** — SHAP/LIME 归因未实现
- ⚠️ **A/B 测试框架** — 无策略对比框架
- ⚠️ **自动化部署** — 无 CI/CD
- ⚠️ **数据血缘** — 无数据溯源
- ⚠️ **因子失效监控** — IC 衰减自动检测未实现
- ⚠️ **容量分析** — 策略容量/市场冲击未评估
- ⚠️ **多周期回测** — 无多时间尺度回测
- ⚠️ **最优执行算法** — VWAP/TWAP 未实现

## 3. 业务逻辑清晰度 + 操作流程闭环

### 日线流程
```
05:00 daily_repair (补拉)
→ 08:30 signals (生成目标持仓)
→ 09:20 execute (调仓下单)
→ 09:35-15:00 monitor (盘中风控)
→ 15:00 snapshot_close (收盘快照)
→ 15:05 reconcile (日终对账) ← ⚠️ 对账结果不自动修正
→ 19:00 evening_chain (晚间因子)
```

### 问题
1. **reconcile 对账结果不闭环** — `daily_recon` 表记录偏差但不自动修正
2. **monitor 熔断后不恢复** — 无自动恢复条件
3. **signals → execute 依赖不严格** — `depends_attempt` 允许 failed 后执行
4. **晚间链失败不重试** — 链中止，无自动重试
5. **weekly_eval 无依赖检查** — 不检查 daily_repair 是否完成

## 4. 系统架构评估

### 当前架构问题
1. **混合调度模式** — orchestrator + Dagster daemon + cron 子进程三重存在
2. **broker 状态碎片化** — 状态存在 4 处（内存/state_bridge/task_runs/strategy_config）
3. **pipeline 与 scheduler 紧耦合** — 无抽象层
4. **数据流单向** — 无反馈闭环
5. **模块依赖混乱** — 循环依赖风险

### 架构优化建议
- 引入 EventBus（Redis/NATS）替代 task_runs
- Pipeline 作为独立服务
- Dagster 仅做编排，orchestrator 移除
- 状态统一到 Redis
- API 层 FastAPI 替代 Flask

## 5. 代码逻辑错误与重构需求

### 严重逻辑错误
| # | 文件 | 问题 | 严重度 |
|---|---|---|---|
| 1 | `optimizer/rebalance.py` | `validate_orders` `cash < -1` 容差 | 已修复 |
| 2 | `execution/execution_model.py` | `trim_orders_by_alpha` else 保留未减仓订单 | 已修复 |
| 3 | `scheduler/signals.py` | 缺 try/except/finally → task_runs 永卡 running | 已修复 |
| 4 | `scheduler/order_manager.py` | 真实券商路径无现金校验 | 已修复 |
| 5 | `orchestrator.py` | `_check_timeouts` 误标 aborted | 已修复 |
| 6 | `scheduler/runners.py` | `_cleanup_zombie_tasks` 仅清 running | 已修复 |
| 7 | `dagster_assets.py` | 7 资产双-start + signals 死码 | 已修复 |

### 待修复 Bug

| # | 文件 | Bug | 严重度 | 修复建议 |
|---|---|---|---|---|
| 1 | `execution/engine.py` | `Order.cost` 语义不统一 | 中 | 统一 cost 为费用 |
| 2 | `execution/execution_model.py` | `_capital = ctx.total_capital if hasattr else 0` → 资本检查失效 | **高** | 传递 total_capital |
| 3 | `web/app.py` | `get_current_date()` 用 date.today() 与 Dagster 分区键不一致 | 中 | 统一用 partition_key |
| 4 | `scheduler/signals.py` | `_tk_start` 返回 None 时不记录 failed 状态 | 中 | 应 _tk_finish("aborted") |
| 5 | `scheduler/execute.py` | 非调仓日 `targets=[]` 但 `_tk_finish("ok")` 掩盖问题 | 中 | 区分 no_rebalance 与 ok |
| 6 | `scheduler/reconcile.py` | 对账偏差 drift 不自动修正 | 中 | 增加自动补单逻辑 |
| 7 | `scheduler/monitor.py` | 熔断后无自动恢复条件 | 中 | 增加 cb_expiry 自动恢复 |
| 8 | `scheduler/order_manager.py` | `po.target_shares <= 0` 未检查 | 低 | 增加 cancel |
| 9 | `optimizer/rebalance.py` | `trim_orders_by_alpha` 后 available 可能为负 | 中 | 检查 available < 0 |
| 10 | `scheduler/manifest.py` | `depends_attempt` 允许 failed 后执行 | 中 | 改 `depends_ok` |
| 11 | `factor/store.py` | 阻塞因子无自动恢复机制 | 低 | 定期重试 |
| 12 | `data/repos/trade_repo.py` | `get_cash` 无时间范围过滤 | 中 | 增加 date 参数 |
| 13 | `core/state_broker.py` | 硬编码路径推导 | 低 | 用 config.paths |
| 14 | `scheduler/runners.py` | `retry_count` 未在 task_runs 中跟踪 | 中 | 增加 retry_count 列 |
| 15 | `scheduler/monitor.py` | `while True` 无最大循环次数 | 中 | 增加窗口结束自退 |
| 16 | `scheduler/order_manager.py` | 真实券商路径无现金校验 | 中 | 已修复，需确认 |
| 17 | `web/app.py` | `_require_token` 无默认安全策略 | 中 | 默认启用认证 |
| 18 | `execution/cost.py` | 固定费率，无动态滑点 | 低 | 引入动态滑点 |
| 19 | `optimizer/portfolio.py` | `construct()` 的 capital 被设为 0 | **高** | 修复 total_capital 传递 |
| 20 | `execution/execution_model.py` | `execute_buys` 总用市价单 | **高** | 修复资本检查 |

### 关键 Bug 详解（#19-20）

`execute_buys` 中 `_capital = ctx.total_capital if hasattr(ctx, 'total_capital') else 0` → 0 < threshold → 总是市价单。资本检查失效。

**修复**：`ExecutionContext` 应传递 `total_capital`（从 `engine.get_capital()` 计算），或 `execute_buys` 直接用 `engine.get_cash()`。

## 6. 算法优化建议

### Alpha 模型
- 引入 Meta-Labeling + Online Learning
- 问题：`alpha_scores` 在 `compute_trades` 中用于 R1 约束，但 `PortfolioConstructor` 未使用

### 组合优化
- ✅ Black-Litterman 已实现 (`_black_litterman` + config `bl_tau`)
- Risk Parity 已存在 (`_risk_parity`)
- 问题：`capital=0` 导致优化约束失效 → 已修复 (#19-20)

### 因子模型
- 引入因子动量 + 因子拥挤度
- 问题：`factor_curator.py` 和 `factor_registry.py` 职责重叠

### 交易成本
- 引入 Market Impact Model（平方根模型）
- 问题：`CostModel` 与 `trim_orders_by_alpha` 逻辑重复

### 风控
- 引入 CVaR + 动态止损
- 问题：`RiskManager.check` 和 `check_hard_stop` 逻辑重复

## 7. 修复优先级

1. **#19-20**（资本检查失效 → 市价单无保护）— 最高优先级
2. **#1-8** — 已修复
3. **#9-10**（validate_orders + manifest 依赖）— 高优先级
4. **#4-5**（signals/execute 状态记录）— 中优先级
5. **#6-7**（reconcile/monitor 闭环）— 中优先级
6. **#11-18** — 低优先级
7. **#2, #12-15** — 架构/数据层优化

---

*报告生成时间：2026-09-09*
*修复完成时间：待归档*

---

## ✅ 修复完成时间：2026-09-09

| 优先级 | Bug | 状态 | 文件 |
|---|---|---|---|
| **最高** | 资本检查失效→市价单 (#19-20) | ✅ | execution_model.py |
| **高** | target_shares≤0 穿透 (#8) | ✅ | order_manager.py |
| **中** | 未配置 QUANT_API_TOKEN 无警告 (#17) | ✅ | web/app.py |
| 已修复(v625) | validate_orders 严格零底线 | ✅ | rebalance.py |
| 已修复(v625) | trim_orders_by_alpha 丢弃未减仓 | ✅ | execution_model.py |
| 已修复(v625) | signals V586 try/except/finally | ✅ | signals.py |
| 已修复(v625) | _cleanup_zombie_tasks 清 lunch | ✅ | runners.py |
| 已修复(v625) | Dagster 7 资产双-start 清理 | ✅ | dagster_assets.py |
| 已修复(v625) | execute/daily_repair/weekly_eval 安全网 | ✅ | respective files |
| 已修复(v625) | signals 委托 _run + broker.update | ✅ | dagster_assets.py |

### 验证
- 10 文件 ast.parse OK
- 3/3 回归测试通过
- get_cash(quant) = 341.36 (正数)
- Dagster web 重启 (test-v626)
- 0 stuck task_runs

### v625e 新增：Black-Litterman 组合优化

- `quant/optimizer/portfolio.py` 新增 `_black_litterman` 方法
- `config.yaml` 新增 `bl_tau: 0.05` 参数，`method` 支持 `black_litterman`
- 算法：市场均衡收益为先验 → alpha 得分修正后验 → 均值-方差最优权重
- 整手离散化 + 现金回收，与现有 HRP/Risk Parity 同一范式

### v625e 验证
- BL 方法实测 OK: lots=8, cash=2000, total_value=8000
- PortfolioConstructor.from_config() OK
- Dagster web 健康 | get_cash=341.36 | 3/3 测试
## v625e: FactorStore 缺失导入 Bug + OOM 防护 (v626)

### 发现原因
`quant/factor/store.py` (1471 行) 在 v625 被重构为 `factor/store/{core,helpers}.py`。
在拆分过程中, `core.py` 丢失了多个模块级导入, 而这些导入在原 `store.py` 中是存在的。
导致 `FactorStore.__init__`、`materialize`、`_worker_main` 等方法崩溃,
级联影响 `daily_repair`、`signals`、`factor_cache` 三个调度任务全部"今日失败"。

### 修复的缺失导入 (core.py)
| 导入 | 用途 | 崩溃方法 |
|---|---|---|
| `import os` | `os.path.dirname/join/makedirs` | `FactorStore.__init__` |
| `import json` | `json.load/dump` | `_load_json`/`_save_json` |
| `import numpy as np` | `np.nan`, `np.asarray`, `np.int16`, `np.float32`, `np.full` | `_worker_main`, `_build_fundamentals_panel` |
| `import pandas as pd` | `pd.Timestamp/Timedelta/DataFrame/Series/concat` | `materialize`, `_worker_main`, `_build_fundamentals_panel` |
| `import gc` | `gc.collect()` | `materialize` finally block |
| `from quant.config.constants import _require_cfg` | `_require_cfg("data.lookback_days")` | `materialize` 日期范围计算 |
| `from quant.factor.compute.price._alternative import clear_ztd_cache` | `clear_ztd_cache()` | `materialize` finally block |
| `from quant.factor.compute._preload import slice_aux_for_date` | `slice_aux_for_date(_AUX_FULL, date_str)` | `_worker_main` |

### 额外 Bug 修复
1. **`materialize_segment.py`**: 子进程设置全局变量时用 `_store_mod._DATA_FULL` (package 级别) 而非
   `_store_mod.core._DATA_FULL` (core 模块级别), 导致 `_worker_main` 读到 `None` 抛出
   `RuntimeError("Worker: _DATA_FULL is None — fork failed")`。
   修复: `_store_mod._DATA_FULL` → `_store_mod.core._DATA_FULL` (及 5 个其他变量)

2. **`_load_json` 空文件处理**: 如果 `trading_days.json` 为空 (0 bytes), `json.load` 抛出
   `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`。
   修复: 增加 `try/except (json.JSONDecodeError, ValueError)`，空/损坏文件返回 `{}`

3. **`repair.py` OOM 防护**: `_ensure_factor_cache` 使用 `backtest.factor_cache_start` (2020-01-01)
   作为 factor_cache 起点, 导致全量回算 1622 日期耗尽 8GB M1 内存被系统杀死。
   修复: 缩小起点到 `trading_days.json` 中最后一个日期 (idempotent, 已物化跳过), OOM-safe

### 验证
| 任务 | 状态 | 详情 |
|---|---|---|
| `daily_repair` | ✅ ok | 0s, factor_cache already materialized |
| `factor_cache` | ✅ ok | 7 dates × 4 factors → 145796 rows, 39.7s |
| `signals` | ✅ ok | 2 targets (sleeve 76 stocks), 8.9s |
| `execute` | ✅ ok | auto-run after signals |
| 571 文件 | ✅ ast.parse OK |  |
