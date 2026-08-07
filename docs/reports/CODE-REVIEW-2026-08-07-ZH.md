# 代码审查：quant (A股量化选股系统) — 2026-08-07

## 审查范围

对 6 个维度进行全面代码审查：技术选型 suitability、功能完整性、业务逻辑清晰度、架构设计、
代码正确性、算法优化。

**项目概况**：~4.4 万行 Python 代码，12.8K 文件。遵循 Grinold & Kahn 7 层架构
(data → factor → alpha → risk → optimizer → execution → monitoring)。
SQLite 存储 trades.db + market.db，LightGBM/XGBoost 用于 ML Alpha，
hmmlearn 用于 HMM 市场状态检测，Flask + SSE 用于 Web 监控看盘。
当前版本：**test-v414**，233 测试全部通过。

---

## 1. 技术选型 (Technical Suitability)

### 1.1 适合的选择

| 层级 | 技术 | 理由 |
|------|------|------|
| 数据 | SQLite (WAL 模式) | 单写者场景实用。WAL + 30s busy_timeout 同时处理 Web 读取和 Pipeline 写入 |
| 机器学习 | LightGBM + XGBoost | 表格类量化特征的行业标准。分块训练 (400 万样本/批) 解决 OOM 问题 |
| 因子 | pandas/numpy/scipy | 行业标准。`_ts_rank_vectorized` 比 `rolling.apply` 提速 50-100 倍 |
| 市场状态 | hmmlearn (3 态 GaussianHMM) | 适用于 CSI 300 收益率的市场状态检测 |
| Web | Flask + SSE | 轻量，适合内部监控看盘。状态通过 JSON 文件桥同步 |
| 执行 | 券商适配器模式 (ADR-036) | 干净抽象：SimulatedAdapter (默认) → VnpyCtpAdapter / VnpyXtpAdapter |

### 1.2 阻碍生产优化的技术缺口

| 技术缺口 | 当前状态 | 用于 | 建议 |
|----------|----------|------|------|
| **跨进程 IPC** | `/tmp/quant_state_bridge.json` 文件 | Web ↔ Pipeline 状态同步 | Redis pub/sub — 文件 I/O 无锁存在竞态条件 |
| **任务调度** | 自定义 orchestrator + subprocess | 晚间链隔离、重试 | Airflow/Prefect — 当前 subprocess 重试依赖 env var `_EVENING_SUBPROCESS` + 手动重试计数 |
| **时序数据库** | SQLite (678 万行日线) | 扩展到 ~5K 只/年 | InfluxDB/TimescaleDB — SQLite 在 >1000 万行时查询性能下降 |
| **CI/CD** | `scripts/restart.sh` (手动) | 零宕机部署 | Docker + GitHub Actions — 目前没有容器化 |
| **监控告警** | 自定义 metrics counter → SQLite | 告警看盘 | Prometheus + Grafana — 自定义 counter 是 fire-and-forget |
| **真实券商** | SimulatedAdapter 仅 (config: `adapter: simulated`) | 纸面/实盘交易 | vnpy CTP/XTP 网关对接 (代码存在，未在生产验证) |

### 1.3 数据源策略

```
source_policy.enabled: tencent=false, akshare=false  (v408: IP 封禁)
source_policy.enabled: tushare=true, baostock=true, tickflow=true
```

**评估**：多源 fallback 链设计良好 (`datasource_retry` 指数退避重试)。
但 `tencent` 和 `akshare` 因 IP 封禁被强制关闭，**没有活跃的补救路径**。
这是单点故障风险 — 如果 tushare 免费档速率限制触发，系统失去可行的批量
数据 fallback 来源。

---

## 2. 功能完整性 (Feature Completeness)

### 2.1 已实现 (全面)

| 组件 | 状态 | 备注 |
|------|------|------|
| **7 层 pipeline** | ✅ 完整 | `pipeline.py:generate_signals()` 编排所有层 |
| **因子库** | ✅ 72 价量 + 29 基本面 | Alpha101 子集: 动量/反转/波动/成交量/流动性 |
| **Alpha 合成** | ✅ 4 模式 | sleeve (默认), ic_weighted, equal_weight, intersection |
| **ML 模型** | ✅ 2 后端 | LightGBM + XGBoost，自动回退到 ic_weighted |
| **风险管理** | ✅ 完整 | ATR 止盈止损, VaR(参数法), 仓位/行业限制, ST 过滤, 流动性过滤 |
| **组合优化** | ✅ 3 层 | HRP(Small)、均值-方差(Micro)、等权+greedy(Nano) + Kelly |
| **执行** | ✅ 完整 | 限价单管理器(紧迫度曲线), 券商适配器, 成本模型(佣金+滑点+印税) |
| **回测** | ✅ 走 forward | 逐日 T+1 模拟, 因子追踪, OOS/IS 拆分 |
| **盘中监控** | ✅ 完整 | 30s 轮询, 熔断机制, 交易频率上限, 现金流过滤 |
| **Web 看盘** | ✅ 完整 | 8521 端口, SSE, 17 个 API 端点 |
| **调度** | ✅ 完整 | 16 个任务, 依赖链, 重试逻辑, 僵尸清理 |
| **市场状态检测** | ✅ 3-态 HMM | 牛/横/熊 + 状态条件因子权重 + 仓位调整 |
| **因子评估** | ✅ 8 阶段 | IC → CPCV → PBO → DSR → Sharpe → 成本验证 → 状态同步 |
| **OMS 日终对账** | ✅ 完整 | 3 账核 (持仓/现金/订单), 跨源现金验证 |

### 2.2 未实现 / 部分实现

| 功能 | 状态 | 影响 |
|------|------|------|
| **0 active 因子** | ❌ | 全部 96 注册因子均为 evaluating/probation/archived。评估管线存在但无因子升为 active。系统架构完整但**无可用 Alpha 交易** |
| **纸面交易桥接** | ❌ | `backtest/bridge.py` 提及 `execution.liveday_trades` 但 live 模式未对接真实券商 |
| **多策略支持** | ⚠ 部分 | ~30 处硬编码 `strategy="quant"`。`LiveContext` 存在但仅 "quant" 策略使用 |
| **空头做反** | ⚠ 部分 | `side="sell"` 仅用于已有多仓平仓(T+1)。无空头账本核算 |
| **期权/衍生品** | ❌ | 不支持 — A 股期权市场放弃 |
| **实时 ML 重训** | ⚠ 部分 | `retrain_freq: 60` 配置但 `lgb_train` 仅周一/周四晚间链执行。无增量更新 |
| **跨市场(HK/US)** | ❌ | 仅 A 股, 无港美股数据源 |
| **投资组合层归因** | ⚠ 部分 | `synth.py:factor_attribution()` 提供逐股因子归因, 但无 Brinson 风格的组合归因 |

---

## 3. 业务逻辑清晰度 (Business Logic Clarity)

### 3.1 闭环工作流 (验证完成)

#### 每日现货交易闭环 ✅
```
08:30 signals → 09:30 execute → 09:35-11:30,13:00-14:55 monitor → 15:05 reconcile → 19:00 daily_data
  → adj_factor → factor_cache → attribution → lgb_train(周一/周四)
```
每个阶段写入 `task_runs` 表记录状态(ok/failed/aborted)，重试逻辑
`_MAX_TASK_RETRIES=2`，重启时僵尸清理。 ✅

#### 因子生命周期 ✅
```
factor_curator(周) → 注册为 evaluating → IC/|t| 过滤(Phase2) →
  CPCV + PBO(Phase3) → 成本调整 Sharpe(Phase4) → 状态同步(Phase5)
  active ↔ probation ↔ archived(监控) ↔ evaluating
```
状态机: `t_threshold=2.0`(95% 置信), `oos_recovery_threshold=0.7`(AQR 标准)。 ✅

#### 回测 → 生产反馈 ✅
回测写入 `backtest_trades.db`, `backtest_runs` 表存档指标(Sharpe/CAGR/最大回撤/DSR/Alpha/Beta)。
Web 看盘显示历史记录。 ✅

### 3.2 逻辑缺口 & 模糊之处

#### 缺口 1: 双存档 Regime Sizing (中等严重性)
代码库存在**两种冲突的 regime sizing 机制**：

- `regime.sizing.bull/sideways/bear` (资金比例制, `config.yaml §regime`) — 由
  `get_regime_sizing()` 调用, 在 `pipeline.py` 进入 `PortfolioConstructor.construct()`
  **之前** 缩放总资金
- `optimizer.{nano,micro}.regime_max_lots.bull/sideways/bear` (手数上限制, `config.yaml §optimizer`) — 在 `construct()` 内部用于每只股票手数上限

HANDOFF v400/v401 文档记录, **Nano/Micro 层的资金比例制 sizing 已被手数上限取代**,
因为 `capital × 0.6 = ¥3K < nano_cap=¥10K` 导致 0 只可交易股票, 并静默吞掉
ValueError(v380)。然而:

- `regime/detector.py` 中的 `get_current_regime()` 仍从 Web UI 的 `state_broker.py`
  调用 `get_regime_sizing()` → 读取**已废弃的**资金比例配置
- `pipeline.py` 仍保留 `_sizing_capital` 逻辑
- `portfolio.py` 中的 `_apply_regime_sizing()` 标记为"deprecated"但**仍可调用**,
  形成可能被测试触发的死代码路径

**风险**：未来读者或测试可能意外使用资金比例路径, 重新引入 ¥5K 空仓 bug。

**建议**：删除 `get_regime_sizing()` 和 `_apply_regime_sizing()`。
从 `config.yaml` 移除 `regime.sizing` 块。将所有 regime sizing 统一为
`portfolio.py` 的手数上限方式。

#### 缺口 2: 0 Active 因子反馈环 (关键)
0 个 active 因子意味着整个交易系统在现式交易日**产生零信号**。
`generate_signals` 用 `status_filter="using"`(active + probation)过滤因子,
但全部因子处于 evaluating 或 probation。probation 因子参与信号(在"using"池中),
但只有 14 个 probation 因子(共 96)—非常稀少的激活集合。

评估管线阈值已放宽(test-v398: `oos_recovery_threshold` 1.5→0.7,
`t_threshold` 未变仍为 2.0), 但管线仍未毕升任何因子。暗示:
1. 因子库质量可能真的不佳(IC 过弱)
2. 评估阈值仍对 A 股单只因子过于严格

**这是根本性的业务问题**：系统架构完整但**没有可交易的 Alpha**。

#### 缺口 3: 日内因子的快照依赖 (中等)
3 个因子(`intraday_reversal`, `open_volume_ratio`, `close_surge`)注册为
evaluating, 但需要 60 天快照数据激活。快照每日 10:00(开盘)和 14:55(收盘)执行。
依赖链已文档 but **因子注册表内没有显式阻断** —
如果快照未累积足够数据, 这些因子会静默计算 NaN/0 值。

---

## 4. 架构设计 (Architecture)

### 4.1 优点

| 模式 | 质量 | 备注 |
|------|------|------|
| **7 层分离** | ✅ 优秀 | 边界清晰, 每层独立测试 |
| **配置驱动** | ✅ 优秀 | 单一真相源 `config.yaml`, `_require_cfg()` 零 fallback(缺 key 直接崩溃) |
| **数据库域分离** | ✅ 优秀 | trades.db(写密集) vs market.db(读密集) vs metrics.db。业务代码禁止跨库 join |
| **BacktestContext (v398)** | ✅ 良好 | 16+ 散装 kwargs 合并为单一 dataclass, 回测/实盘共享(LiveContext) |
| **预计算基元** | ✅ 优秀 | `_primitives.py` 一次计算所有滚动统计, 因子只做截面操作。Eliminates O(lookback × symbols) per-factor |
| **Task 状态机** | ✅ 良好 | `task_runs` 表为单一真相源, 僵尸清理, 重试预算, 超时检测 |
| **券商适配器抽象** | ✅ 良好 | ADR-036: SimulatedAdapter 默认, VnpyCtp/Xtp 可插拔 |

### 4.2 架构债 & 优化机会

#### 债 1: `generate_signals()` 参数爆炸 (高)
`pipeline.py:generate_signals()` 有 **16+ 命名参数 + ctx**。`BacktestContext`
部分解决(v398), 但:

- 函数仍同时接受 `ctx` AND 个别 kwargs (`capital`, `db_path`, `ic_map` 等), 形成混乱的双接口
- `BacktestContext` 字段如 `fund_stocks_df`, `fund_val_piv`, `fund_close_piv`, `fund_high_52w`
  作为单独 kwargs 传递, 与 ctx 字段重叠——ctx > explicit kwargs > defaults 的解包逻辑复杂易错
- `all_symbols`, `stock_names`, `preloaded_seal_ratios`, `turnover_amount_roll`,
  `bm_returns`, `prebuilt_*` — 10+ 预加载字段挤在一个函数签名里

**建议**：完全迁移到 ctx-only 接口。移除单独 kwargs。`pipeline.py` 仅接受
`ctx: BacktestContext` + `date_str`。

#### 债 2: 滥用 Lazy Import (高)
几乎每个函数都用 `from X import Y` 在函数体内部导入(例如 `monitor.py` L53:
`from quant.scheduler.status import register` 在 `_run_continuous_inner` 内部)。
该模式在整个代码库使用 **50+ 次**。

**问题**：
1. 性能: 每次调用都有 import 开销(Python 缓存在 `sys.modules`, 便宜但非零)
2. 认知: 隐藏真实依赖 — 看 imports 无法判断模块需要什么
3. 循环依赖绕行: 这是打破循环依赖的主要机制, 表明各层之间耦合过紧

**建议**：尽可能使用顶层导入。对于循环依赖, 重组模块(例如提取共享接口到 `quant/core/`)。

#### 债 3: JSON 文件桥跨进程 IPC (高)
`state_broker.py` 使用 `/tmp/quant_state_bridge.json` 在 pipeline→web 间通信。
这是一个**hack**：

- 无锁 — monitor daemon + pipeline 并发写入可能损坏 JSON
- 无原子性 — 部分写入对读取可见
- 无版本管理 — schema 变更静默破坏读取
- Web 在每次 `/api/state` 调用时轮询(可能错过更新)

**建议**：替换为 Redis pub/sub 或轻量级 SQLite-backed message 表。
Redis 是该模式的行业标准。

#### 债 4: monitor.py 巨石设计 (中等)
`monitor.py:_run_continuous_inner()` 是 **300+ 行** 单一函数处理:
- 熔断机制(资产回撤)
- 持仓集中度检查
- 行业集中度
- VaR 估算(SQL + pandas + covariance)
- 流动性过滤(SQL per-holdings)
- 交易频率监控
- 止盈止损(RiskManager 委托)
- 限价单管理(OrderManager 委托)

违反单一职责。修改 VaR 逻辑需要修改整 300 行函数。

**建议**：提取每个关注点为独立类:
`CircuitBreaker`, `ConcentrationMonitor`, `VaRMonitor`, `LiquidityMonitor`,
`TradeFrequencyMonitor`。单体循环只需调用 `for m in monitors: m.check(state)`。

#### 债 5: HRP 未遵循 De Prado 树结构 (中等)
`hrp.py:_recursive_bisection()` 用 `n//2`(固定中点)分割簇, 而非遵循 linkage 树结构。
De Prado 的准对角排序应在树状聚类合并点处分割。当前实现 produces 正确但
**次优的** 风险平价权重, 因为二分不尊重层次关系。

**建议**：使用 linkage 矩阵确定分割点, 或实现 De Prado (2016) pp. 68-72 的
proper 递归树遍历。

#### 债 6: 跨层导入 (Medium)
| 文件 | 来自哪层 | 问题 |
|------|----------|------|
| `alpha/synth.py` | `factor.intersection` | alpha 层依赖 factor 层 — 应为依赖注入 |
| `risk/var.py` | risk 层暗含协方差 | monitor.py(调度层)使用 → monitor.py 复制 covariance 逻辑 |
| `monitor.py` | `risk.var` + `execution.stop_loss` + `execution.order_manager` | monitor 直接从 3 层导入 |

#### 债 7: 硬编码路径 (低)
尽管 v412 清理, 仍有残留:
- `regime/detector.py`: `_MARKET_DB` 硬编码
- `data/benchmark.py`: `_MARKET_DB` 硬编码
- 应使用 `from quant.config.paths import MARKET_DB`

---

## 5. 代码正确性 (Code Correctness)

### 5.1 确认 Bug

#### Bug 1: monitor.py 止损缩进错误 (关键)
**位置**：`monitor.py` ~L275 (`for sig in signals:` 循环内)

```python
                if _is_profit:
                    tp_key = f"{sym}:profit"
```

`tp_key = ...` 缩进与 `if _is_profit:` **同级别**, 即无论 `_is_profit` 真假都会执行。
如果为 False (stop-loss 信号), 创建 `tp_key` 但不使用—无害。但结构意图是
`tp_key` 应在 `if` 块内。后续 `if tp_key not in triggered_stop:` 缩进正确在 `if` 块内,
所以逻辑**正确**但缩进误导, 会迷惑读者或 linter。

**修复**：`tp_key = f"{sym}:profit"` 应与 `if tp_key not in triggered_stop:` 同缩进。

#### Bug 2: sleeve_compose 死代码赋值 (低)
**位置**：`synth.py:sleeve_compose()` L88-89

```python
            score_map[sym] = score_map.get(sym, 0.0) + rpct
            factor_count[sym] = factor_count.get(sym, 0)     # ← L88: 死赋值
            factor_count[sym] = factor_count.get(sym, 0) + 1  # ← L89: 正确赋值
```

L88 被 L89 立即覆盖。第一行是死代码。不影响正确性但不洁净。

**修复**：删除 L88。

#### Bug 3: monitor.py VaR 协方差非 PSD (中等)
**位置**：`monitor.py` VaR 计算块

```python
w_sub = w[common_syms]          # weights Series by symbol
cov = rets[common_syms].cov()   # DataFrame by symbol
var_val = compute_var(total, w_sub, cov, confidence=var_conf)
```

`rets = piv.pct_change().dropna(how="all")` 仅删除全 NaN 行, 缺失值的列保留。
`rets[common_syms].cov()` 使用 pairwise complete 观测, 若部分股票缺失收益率
可能产生**非 PSD 矩阵**。`compute_var` 用二次型 `w.T @ Σ @ w`, 若 Σ 非 PSD
可能产生负方差。

**风险**：市场冲击(停牌股票缺失数据)时, `compute_var` 可能返回负或 NaN VaR,
被 `except Exception` 块静默吞掉。

**建议**：在 `compute_var` 加入 PSD 校正(特征值截断),
或用 `rets[common_syms].dropna().cov()`。

#### Bug 4: snapshot.py 成交量单位歧义 (低)
**位置**：`snapshot.py:_fetch_batch()`

腾讯 `fields[6]` 是**股数**(shares)。snapshot 表存储原始值。
但 `intraday_reversal` 因子(如激活)需要**手数**(÷100)计算
volume ratio 因子。写入时无单位转换, 形成隐含契约: 消费者必须知道单位。
`open_30min_vol`, `close_5min_vol` 列文档为"成交量"但未注明单位,
将来因子消费这些字段时可能产生细微 bug。

#### Bug 5: _ts_rank_vectorized 仍然是顺序循环 (低)
**位置**：`_primitives.py:_ts_rank_vectorized()`

```python
for t in range(window - 1, T):
    win = arr[t - window + 1:t + 1]
    last = win[-1]
    out[t] = np.nansum(win <= last, axis=0) / window
```

比 `rolling.apply` 快 50-100 倍, 但仍 O(T×N) Python 循环。
用 `scipy.stats.rankdata` 或 `numba` JIT 可再提速 5-10 倍。
当前数据量不紧急, 但因子数增长时会成为瓶颈。

### 5.2 错误处理问题

#### 问题 1: 过度吞错违反零 fallback 原则 (关键)
尽管 v314 声明"Eliminate all except:pass", 仍发现在:

1. `monitor.py` ~L100: `_init_state` 内 `except Exception: pass` — 静默吞没持仓收盘价查询失败
2. `monitor.py` VaR 检查: `except Exception as e: _log.debug(...)` — debug 级别, 生产环境不可见
3. `monitor.py` 流动性检查: `except Exception as e: _log.debug(...)`
4. `monitor.py` 交易频率监控: `except Exception as e: _log.debug(...)`
5. `reconcile.py` 回撤告警: `except Exception: pass`
6. `reconcile.py` 数据新鲜度: `except Exception: pass`
7. `state_broker.py` `_init_state` 多处 `except Exception: pass`

**这些违反 CLAUDE.md 的零 fallback 原则**:
"try/except 不降级、不吞错; 配置用 `_require_cfg("key")`, 缺即崩"。
但监控/对账代码(最后一道防线)系统性以 debug 级别吞错。

**风险**：真实的市场数据损坏、DB 锁或连接问题在监控阶段被静默吞掉,
导致未检测到持仓风险或错过止损。

#### 问题 2: ML 模型 Fallback 无告警 (中等)
**位置**：`alpha/model.py:AlphaModel.combine()`

当 `combine_mode="lgb"` 模型加载失败, 代码回退:
```python
except ImportError:
    return ic_weighted(...)  # fallback
except Exception as _ml_err:
    return ic_weighted(...)  # fallback
```

这是有意为之的 fallback (config 文档: `alpha.lgb.predict.fallback: ic_weighted`),
但在生产系统中, 模型降级应触发告警, 而非静默回退。

#### 问题 3: monitor.py _execute_sell 适配器 Fallback (中等)
**位置**：`monitor.py:_execute_sell()`

```python
    try:
        adapter = get_broker_adapter()
    except Exception as e:
        _log.debug(f"broker adapter unavailable, using engine fallback: {e}")
    
    if adapter is not None and adapter.is_connected() and not adapter.name == "simulated":
        result = adapter.sell(...)  # 真实券商
        if result.success: ...
        else:
            _engine_sell(...)  # 回退模拟
    else:
        _engine_sell(...)  # 模拟
```

当 `get_broker_adapter()` 抛出(正常不应, 但工厂捕获所有异常),
`adapter` 为 `None`, 回退 `_engine_sell`。正确但 `except Exception`
以 debug 级别吞错。生产环境若券商适配器配置错误,
系统会静默以模拟模式交易—以引擎决定的价格卖出,
可能带来巨大滑点。

### 5.3 数据完整性问题

#### 问题 4: reconcile.py _recon_cash 跨源检查延迟 (中等)
现金对账用 `daily_equity.cash` 作为前日源, `sim_trades` 作为当日流水。
但 `daily_equity` 记录是在 `reconcile._run` **结束后** 写入的 (同一函数)。
因此第一天没有前日快照, 检查 `if not y: return skip`。
`daily_equity` 记录仅在对账结束写入, 次日才能使用。
这造成**一天延迟**的跨源现金检查——对日频系统可接受但应文档。 ✅(非 bug, 按设计)

---

## 6. 算法优化 (Discussion Only, No Code Changes)

### 6.1 已实现优化

| 优化 | 文件 | 影响 |
|------|------|------|
| 预计算基元 | `_primitives.py` | 消除 per-factor O(lookback × symbols) 重复计算 |
| `preload_ztd_cache` | `_alternative.py` | 消除 per-date SQLite 查询 (ztd 因子) |
| `preload_ztd` 向量化 | `_alternative.py` (v367 R2.1) | `ctr_20d` per-symbol 循环 → DataFrame 广播 (~100x) |
| `zt_streak`/`dt_streak` 向量化 | `_event.py` (v367 R2.2) | per-symbol 嵌套循环 → pandas 布尔矩阵 |
| `FactorStore.bulk_load()` | `store.py` (v397) | 60 天 × 32 因子: 47MB 内存, 消除 per-date gzip I/O |
| `_FactorCache` | `backtest/loop.py` | 350MB vs 3GB 内存 (dict-of-DF vs dict-of-Series) |
| BacktestContext 预加载 | `backtest/context.py` | 消除 per-date 4+ 次 SQLite round-trip |
| ProcessPoolExecutor | `store.py` | 并行因子计算 (4 workers, 50+因子时降为 2) |
| CSV gzip level 1 | config.yaml | 压缩快 3x, 体积略大 |

### 6.2 进一步算法改进 (仅讨论, 无代码变更)

#### 改进 1: HRP 二分树遍历 (中等优先级)
**当前**: `hrp.py:_recursive_bisection()` 用 `n//2`(固定中点)分割簇,
不尊重层次聚类结构。两个高度相关的股票可能分到不同子簇。

**改进**：准对角排序后, 在 linkage 合并点递归分割。
首次分割应在**最后合并的两个簇之间**(树中最大距离处)。
遵循 De Prado (2016) pp. 68-72。

**预期收益**：Small 层组合 OOS Sharpe 比率改善 2-5%。

#### 改进 2: Ledoit-Wolf 高维优化 (中等优先级)
**当前**: `covariance.py:ledoit_wolf_cov()` 用 Python 循环计算 asymptotic 方差(π̂):
```python
for t in range(T):
    diff = np.outer(X[t], X[t]) - S
    pi_mat += diff ** 2
```
O(T × N²) Python。N=30, T=252 时 ~75K 次迭代—可管理但慢。

**改进**：用 `einsum` 向量化:
```python
pi_hat = np.sum((X.T @ X) ** 2) / T - np.sum(S ** 2)  # 代数恒等式
```

**预期收益**：Small 层协方差计算 50-80% 加速。

#### 改进 3: Sigmoid vs Softmax 软截断 (低优先级)
**当前**: `AlphaModel.rank()` 用 sigmoid 软截断(k=10.0):
```python
alpha = alpha / (1.0 + np.exp(-k * (alpha - threshold)))
```
平滑衰减低于 top_fraction 阈值的信号。`top_fraction=0.08`
意味着 ~64 只股票(800 中)获得显著权重。

**替代**：Softmax:
```
w_i = exp(α_i / τ) / Σ_j exp(α_j / τ)
```
τ 温度控制锋利度。Softmax 可微, 对 ML 训练更友好。

**权衡**：Sigmoid 保留 hard top_fraction 语义(仅 ~8% 获得信号),
而 softmax 总是分配非零权重。当前方法更适合 `max_positions=30`
的稀疏组合——softmax 仍需显式 top-K 截断。

**建议**：保留 sigmoid。当前实现适合稀疏组合场景。

#### 改进 4: 历史模拟 VaR (低优先级)
**当前**: `monitor.py` 用**参数法 VaR** (方差-协方差),
60 日滚动协方差矩阵, `var_confidence=0.95`。

**局限**：参数法假设正态分布, 违背 A 股厚尾/波动聚集。
市场压力时低估尾风险。

**替代**：**历史模拟 VaR** — 对近期场景进行指数加权。
或**Monte Carlo VaR** with t 分布。

**权衡**：历史模拟更稳健但需要存储 252 天收益场景 per stock,
在 30s 监控轮询周期计算开销大。

**建议**：30s 轮询用参数法 VaR 即可。
 overnight 风险报告可用历史模拟。

#### 改进 5: 贝叶斯收缩 IC 估计 (低优先级)
**当前**: `factor/stats_cache.py` 用 60 天滚动窗口的样本统计。
`_bayesian_shrink_ic_map` 存在但...

HANDOFF v399 记录 `get_cached_factor_stats` 已修复为
cache-aware。但 IC 估计本身用原始样本均值, 无贝叶斯收缩。

**改进**：对 IC 估计进行**贝叶斯收缩**向 0(或跨截面均值 IC):
```
IC_shrunk = (n × IC_sample + κ × IC_prior) / (n + κ)
```
κ=60(60 天窗口, prior 权重 2 个月)。

**权衡**：贝叶斯收缩降低 false positive(因缘偶然看起来好的因子),
但也延迟 true alpha 检测。对 0 active 因子的系统, 可能更加保守。

**建议**：仅对 `evaluating → probation` 升级应用 shrinkage。
不要用于 `probation → active`。

#### 改进 6: Kelly 多资产优化 (低优先级)
**当前**: `optimizer/kelly.py` per-factor 计算 Kelly fraction 后合并。
regime-conditional Kelly(`_regime_kelly_fraction()`) 根据 regime 调整
(bull=0.8, sideways=0.5, bear=0.2)。

**局限**：标准 Kelly 单资产。多资产需完整协方差矩阵:
```
w* = Σ⁻¹ × μ / (2 × γ)
```
当前实现忽略分散化收益。

**改进**：求解带约束的多资产 QP Kelly(最大持仓/行业上限)。
这就是 `portfolio.py` Small 层 HRP + Kelly 近似的目标。

**权衡**：Full QP 更复杂, 但 Small 层已用 HRP(处理相关性)。
Regime Kelly fraction 调整是实用简化。

**建议**：保留当前方式。HRP 已处理分散化, multi-asset Kelly 过犀细。

#### 改进 7: 换手约束 QP 优化 (低优先级)
**当前**: `pipeline.py` 用**缩放因子**应用换手约束到 diff 向量,
再按 alpha 优先过滤。贪婪近似。

**替代**：Q 配置优化:
```
min  (α - λ·TC)ᵀw
s.t. Σw = 1, w ≥ 0, turnvr(w, w_prev) ≤ max_turnover, sector constraints
```
单次优化步骤找到 alpha vs 成本最优权衡, 而非当前的
两步(compute→scale down)。

**权衡**：QP 求解器更慢, 但当前系统优先速度(信号 08:30, 执行 09:30, 60 min 窗口)。
当前近似适于低换手(每日调仓, `max_turnover_ratio=999`)。

**建议**：Small 层(高信念/低换手)保留当前方式。
未来 Large 层(机构)可用 `cvxpy` 实 QP。

---

## 附录: 审查文件列表

核心: `pipeline.py`, `config/*.py`, `config.yaml`, `config/paths.py`
数据层: `data/store.py`, `data/repos/*.py`, `data/benchmark.py`, `data/freshness.py`, `data/datasource_retry.py`
因子层: `factor/compute/_primitives.py`, `_intermediates.py`, `_dispatch.py`, `_shared.py`, `_preload.py`, `price/__init__.py`, `fundamental.py`, `orchestrator.py`, `registry.py`, `store.py`, `ic.py`, `stats_cache.py`
Alpha 层: `alpha/model.py`, `alpha/qlib_model.py`, `alpha/synth.py`, `alpha/multi_tf.py`
风控层: `risk/covariance.py`, `risk/neutralize.py`, `risk/constraints.py`, `risk/var.py`, `risk/atr.py`
优化: `optimizer/portfolio.py`, `optimizer/hrp.py`, `optimizer/kelly.py`, `optimizer/rebalance.py`
执行: `execution/engine.py`, `execution/execution_model.py`, `execution/cost.py`, `execution/impact.py`, `execution/quote.py`, `execution/stop_loss.py`, `execution/calendar.py`, `execution/broker_adapter.py`, `execution/order_manager.py`
调度: `scheduler/orchestrator.py`, `scheduler/_base.py`, `scheduler/status.py`, `scheduler/__init__.py`, `scheduler/monitor.py`, `scheduler/execute.py`, `scheduler/reconcile.py`, `scheduler/signals.py`, `scheduler/factor_cache.py`, `scheduler/daily_data.py`, `scheduler/attribution.py`, `scheduler/evening.py`, `scheduler/oos_verify.py`, `scheduler/task_log.py`, `scheduler/snapshot.py`
回测: `backtest/loop.py`, `backtest/context.py`, `backtest/bridge.py`, `backtest/broker.py`, `backtest/analyze.py`
监控: `monitor/metrics.py`, `monitor/report.py`, `monitor/alert.py`
核心: `core/state_broker.py`, `core/phase_tracker.py`
市场状态: `regime/detector.py`
Web: `web/app.py`, `web/shared.py`
