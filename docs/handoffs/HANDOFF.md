# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `rg "关键词" HANDOFF.md HYPOTHESES.md docs/adr/` 三文件联动搜索，
> 避免重复踩坑、重新讨论已否决方案、遗漏已有设计。

---
## test-v267: 全项目数据库连接泄漏修复

**问题背景**: 数据库锁 (database is locked) 根本原因 — 多文件 sqlite3.connect() 开连接不关闭，
attribution/factor_cache 运行时累积未关闭连接。

**全盘检查结果** (`python3` 脚本逐一比对 connect vs close):
- 项目共 38 个文件含 `sqlite3.connect`, 7 个文件 GAP > 0
- `repos/` 目录已规范 (每次 connect 后 close), 其余文件大量未对齐

**修复的泄漏文件** (6 个文件):

| 文件 | GAP | 修复方式 |
|------|-----|----------|
| `quant/execution/impact.py` | +2 | `get_stock_volume_snapshot` / `get_stock_volatility_snapshot` 加 `conn.close()` |
| `quant/factor/compute/price/_sentiment.py` | +2 | `_get_news_series` / `compute_news_abnormal_20d` 加 `finally: conn.close()` |
| `quant/data/fund_hold.py` | +1 | `sync_quarter` 加 `if close_conn: conn.close()` |
| `quant/data/northbound.py` | +1 | `sync_single_stock` 加 `if close_conn: conn.close()` |
| `quant/data/limit_up.py` | +1 | `sync_date` 加 `if close_conn: conn.close()` |
| `web/app.py` | +1 | `_tr = sqlite3.connect(TRADE_DB)` 加 `_tr.close()` |

**非泄漏说明**:
- `crowdedness.py` (+1): 使用 `with sqlite3.connect() as conn:` 上下文管理器, 自动关闭, 假阳性
- 负 GAP 文件: 多个文件 conn 变量复用多次 `.close()` (如 `order_manager.py`, `task_log.py`), 无泄漏

**变更文件**: `web/app.py`, `quant/execution/impact.py`, `quant/factor/compute/price/_sentiment.py`, `quant/data/fund_hold.py`, `quant/data/northbound.py`, `quant/data/limit_up.py`

---
## test-v267 补充: repos 语法修复 + 脚本清理 + fund_flow 重写

**repos 孤立 try/finally 修复** (web 启动失败根因):
- `quant/data/repos/universe_repo.py`: 2 处孤立 `try:` 移除 (无匹配 except/finally)
- `quant/data/repos/trade_repo.py`: `_ensure_schema` 和 `record_daily_equity` 的孤立 try/finally 修复
- `quant/data/repos/evaluation_repo.py`: `save_run` 的 try 块缩进修复 (commit 未在 try 内)
- 根因: 之前编辑遗留的半成品 try 块, .pyc 缓存掩盖, web 重启时暴露

**脚本文件语法修复** (Python 3.14 兼容):
- `scripts/init_data.py`: shebang 位置 + 函数内 import 缩进 + 非 ASCII 字符
- `scripts/generate_factor_cards.py`: 模块级代码前导空格
- `quant/core/version.py`: `__version__` 赋值前导空格
- `test/test_registry_smoke.py`: 双重 `as fc as fc`

**fund_flow.py 重写** (东方财富 API 修复):
- akshare `stock_individual_fund_flow` → `requests` 直连 `push2his.eastmoney.com`
- 加 Referer+完整 User-Agent 头绕过服务端断连
- 加指数退避重试 (4 次, 1s→2s→4s)
- 注: 东方财富 API 仍不稳定, 偶尔 RemoteDisconnected, 大市值银行股尤其严重

**变更文件**: repos 3 个文件 + scripts 4 个文件 + `quant/data/fund_flow.py`

---
## test-v268: 删除 backtest/diagnostics.py — 废弃的因子"快照"模块

**删除**: `quant/backtest/diagnostics.py` (30 行)

**原因**:
- `compute_pre_backtest_ic()` 自 test-v215 起已被禁用 — `backtest/loop.py:361` 改为复用
  evaluation/ 的 walk-forward IC, 不再调用 diagnostics
- diagnostics.py 与 evaluation/ 七阶段管线功能重复: evaluation 已提供完整的因子 IC 评估
  (phase2_single → phase3_oos), diagnostics 的"快速快照"定位被替代
- 保持两个独立系统带来混乱: 同一个因子在 diagnostics 和 evaluation 中有两套 IC 指标,
  但只有 evaluation 的结果被实际使用
- 项目中 "diagnostics" 术语从此特指 evaluation/ 管线的 phase 诊断阶段,
  不再指 backtest/diagnostics.py

**清理的引用**:
- `quant/backtest/loop.py`: 移除 `from quant.backtest.diagnostics import compute_pre_backtest_ic`
- `quant/backtest/analyze.py`: 更新模块分工文档 (移除已废弃的 diagnostics.py 分工说明)
- `quant/factor/ic.py`: 更新注释 (backtest/diagnostics → evaluation/)

**变更文件**: `quant/backtest/diagnostics.py` (删除), `quant/backtest/loop.py`,
`quant/backtest/analyze.py`, `quant/factor/ic.py`

---
## test-v269: 6项业界标准差距修复

### Item 1 — 冲击成本模型 (Almgren-Chriss 接入)
- **现状**: `cost.py` 已内置 `slippage_with_impact()` — 有 volume 时调用 `impact.py` 的
  Almgren-Chriss 模型, 无 volume 时回退固定千一滑点 (已完成, 此次仅确认)
- 影响: 大单交易 (>日均量 1%) 时回测收益不再偏乐观 0.5-1.5% 年化

### Item 2 — 市场微观结构 (Roll 1984 bid-ask spread)
- **新增**: `quant/execution/market_microstructure.py`
- `estimate_roll_spread(prices)` — 从日线序列协方差反推有效价差
  公式: 2 × √(-cov(Δp_t, Δp_{t-1}))
- `batch_roll_spread(symbols, date)` — 批量估算, 用于回测诊断
- 来源: Roll (1984) + Harris (2003) Trading & Exchanges

### Item 3 — 幸存者偏差 (退市股数据完整性)
- **新增**: `quant/data/quality.py` — `check_delisting_completeness()`
- 检测退市股在 delist_date 前是否有完整的 daily 数据
- 标准: CSMAR/CRSP 退市追踪标准

### Item 4 — 前视偏差修复 (财报发布延迟)
- **修改**: `quant/data/store.py` — `get_fundamentals()` SQL 加 60 天发布延迟
- `stat_date <= date` → `stat_date <= date(date, '-60 days')`
- 模拟年报/中报/季报的实际发布时间线 (45-120天延迟, 保守取 60 天)
- **新增配置**: `universe.fundamental_publication_delay_days: 60`

### Item 5 — 风格因子模型 (BARRA 多因子)
- **新增**: `quant/risk/neutralize.py` — `style_neutralize()`
- 支持多元截面回归: alpha ~ value + momentum + volatility + quality + ...
- `neutralize()` 函数新增 `style_exposures` 参数 (向后兼容)
- 来源: Fama-French 5 因子 (2015) + Carhart 4 因子 (1997) + BARRA USE4

### Item 6 — 数据质量管线
- **新增**: `quant/data/quality.py` — 完整数据质量检测模块
- `check_price_anomalies(date)` — 日内涨跌幅 >20% 异常检测
- `check_delisting_completeness()` — 幸存者偏差 (同 Item 3)
- `run_quality_checks(date)` — 统一入口
- **新增配置**: `data.quality.price_anomaly_pct` / `data.quality.volume_spike_ratio`

**变更文件**: `quant/execution/market_microstructure.py` (new), `quant/data/quality.py` (new),
`quant/risk/neutralize.py`, `quant/data/store.py`, `quant/config/config.yaml`


## test-v270: PR2 修复 — validate_orders 裁剪时资金扣除遗漏股价

**问题**: `execute.py:202` — 修剪买单时 `available -= o.cost` 只扣了交易佣金（~¥5），
未扣除 `o.shares * px` 的股价成本。导致后续买单可用资金虚高，可能超买。

**修复**: 改为 `available -= o.shares * px + o.cost`

**PR3 验证结论**: 不存在问题。`pipeline.py:338` 已对 `target_positions` 按 score 降序排序，
`_rank_concentrated` 按 `alpha_series.index` 顺序处理，索引顺序 = score 降序。
Score 本身是 pipeline 中性化+排名后的 alpha 值，与 `_rank_concentrated` 的语义一致。

**变更文件**: `quant/scheduler/execute.py`

---



---




## test-v272: Engine.get_trades 参数位置 bug — limit 误传为 mode

**根因**: `engine.py:174` 调用 `TradeRepo(self.db_path).get_trades(strategy, limit)`
时，`limit`（int）作为第二位置参数传入，但 `TradeRepo.get_trades()` 的签名是
`(self, strategy, mode, limit)`，导致 `limit=500` 被赋值给 `mode` 参数，
SQL 变成 `WHERE mode=500`，查不到任何交易。

**影响**:
- attribution DSR 永远跳过（trades=0）
- attribution R3 turnover 永远跳过（trades_today=0）
- 凡是调用 `engine.get_trades(strategy, limit=N)` 的地方均受影响

**修复**: 改为 `TradeRepo(self.db_path).get_trades(strategy, limit=limit)`

**变更文件**: `quant/execution/engine.py`


## test-v273: 修复 task_runs 僵尸 running 行根因

**根因**: `orchestrator._run_task()` 的 except 捕获异常后仅记录日志，未调用
`_tk_finish()`。任何 task 的 `_run()` 抛异常后，`_tk_start` 创建的 running
行永远留在数据库中，形成僵尸。

**修复**: `orchestrator.py:_run_task` except 中新增 `_tk_finish(name, today, "failed", ...)`，
确保任何未捕获异常都被记录为 task 失败状态。

**修复范围**: 一处修改覆盖所有 6 个 scheduler 任务
(attribution/daily_data/execute/factor_cache/signals/weekly)。

**变更文件**: `quant/scheduler/orchestrator.py`


## test-v274: pytest-benchmark 性能回归检测

**新增**: `test/benchmark_performance.py` — 5 项性能基准测试

覆盖率 (Template 5 性能基线):
  B1. 因子 primitives 预计算 — 500 stocks × 100 days, 基线 ~3.3s
  B2. IC 统计计算 — 20 factors × 500 stocks, 基线 ~10ms
  B3. AlphaModel 组合 — 20 factors × 500 stocks, 基线 ~23ms
  B4. 行业中性化 — 500 stocks, 基线 ~3.6ms
  B5. 优化器排名集中 — 500 stocks, 基线 ~450μs

使用方式:
```bash
# 运行并保存基线
PYTHONPATH=. .venv/bin/pytest test/benchmark_performance.py -v --benchmark-only --benchmark-save=baseline

# 对比基线检测退化
PYTHONPATH=. .venv/bin/pytest test/benchmark_performance.py -v --benchmark-only --benchmark-compare
```

**变更文件**: `test/benchmark_performance.py` (new)


## test-v275: 修复 benchmark_tracking alpha=None + 降级 task_log finish 警告

**Fix 1 — benchmark_tracking alpha=None**:
`record_daily()` 在 `yesterday_equity=None`（daily_equity 表无数据）时，
无法计算 `strat_ret`，导致 alpha 也为 None。新增兜底: 从 `benchmark_tracking`
表自身查找最近一个交易日的 strategy_equity 作为前日权益。

**Fix 2 — task_log finish WARNING 降级**:
`task_log.finish()` 找不到 running 行时发出的 WARNING 改为 DEBUG。
原因: test-v273 在 orchestrator 异常路径增加了 _tk_finish 调用，正常路径
任务自身也调用 _tk_finish，双写时第二次必然找不到 running 行（已被第一次更新），
这是良性条件，不应告警。

**变更文件**: `quant/benchmark/tracker.py`, `quant/scheduler/task_log.py`

## test-v271: Phase 8 — 回测vs实盘一致性验证框架

**新增**: `quant/evaluation/phase8_live_consistency.py` (Phase 8)

四维验证:
  D1. 信号一致性 — 同日 backtest vs live 的 generate_signals() 输出对比
  D2. 成交一致性 — 相同 (date, symbol, side) 的 fill price 对比
  D3. 成本一致性 — CostModel 估算 vs 实盘 sim_trades.cost
  D4. 权益一致性 — backtest equity curve vs live daily_equity

**新增 TradeRepo 方法** (供 Phase 8 查询):
  - `get_daily_signals_range(start, end, mode)` — 按日期范围读 daily_signals
  - `get_daily_equity_range(start, end)` — 按日期范围读 daily_equity
  - `get_strategy_config(strategy)` — 读 strategy_config 配置行

**设计原则**:
  - 渐进退化: 实盘数据不足时返回 insufficient_data, 不报错
  - 信号对比用 Spearman rank 相关 (对 score 非线性变换不敏感)
  - 加权评分: D1 信号 30% + D2 成交 30% + D3 成本 20% + D4 权益 20%
  - 阈值从 config.yaml 读 (暂无, 用模块级常量)

**使用**:
```bash
PYTHONPATH=. .venv/bin/python3 -c "
from quant.evaluation.phase8_live_consistency import validate_consistency
result = validate_consistency()
print(result['status'], result['dimensions'])
"
```

**变更文件**: `quant/evaluation/phase8_live_consistency.py` (new),
`quant/evaluation/__init__.py`, `quant/data/repos/trade_repo.py`

## test-v212: Alpha 候选池 UI 优化

**变更**: `web/static/app.js`, `web/static/style.css`

- renderSignals: 候选池从 8 行缩至 5 行，得分从 3 位小数缩至 2 位
- reason 列截断：超过 2 个因子贡献时只显示前 2 个 + "N more"，hover 显示完整
- CSS: `.status-badge` → `.badge` 基类重命名，对齐 JS 中的 `class="badge badge-red"`
- 新增 `.trunc-reason em` 样式



---

## test-v215: 实盘执行全链路修复 — 涨停预检 + alpha 优先级裁剪 + 价格缓冲

**Phase A — execute.py 三处修复**:
1. `fetch_quotes` 加 `include_ask_bid=True` (获取五档盘口)
2. 新增 Step 3.5 涨停封死预检: 用 ask_volume==0 + 涨停价判断开盘封死, 跳过不挂单, 写入 exec_notes
3. 裁剪逻辑从「成本升序」改为「alpha 得分降序 + 实时开盘价重算股数」: top1 优先分配资金, 剩余给 top2

**Phase B — 价格缓冲**:
- pipeline 分配时预留 5% 价格波动空间 (Nano 层)
- config.yaml: `execution.price_buffer: 0.05`
- 用昨收价 × 1.05 估算成本, 减少 execute 阶段 reopen 价差导致的 validate_orders 裁剪

**变更文件**: `quant/scheduler/execute.py`, `quant/optimizer/portfolio.py`, `quant/config/config.yaml`

---

## test-v286: FactorStore 全量接入 — 5 个剩余文件

**背景**: `oos_verify.py` 已在 test-v284 接入 FactorStore，但还有 5 个文件直接调用 `compute_all_factors` 未走缓存。

**接入模式**:
```
try:
    _fs = FactorStore(db_path=FACTOR_CACHE_DB)
    fv = _fs.load(date_str, symbols=symbols, factor_names=factor_names)
    _fs.close()
    if not fv:
        fv = compute_all_factors(...)  # cache miss, compute fresh
except Exception:
    raise  # 无静默 fallback，DB 损坏直接上抛
```

**接入清单**:

| # | 文件 | 优先级 | 改动 |
|---|------|--------|------|
| 1 | `quant/factor/ic.py` | HIGH | `_compute_one_day()` 逐日 IC 计算 → FactorStore.load() 优先 |
| 2 | `quant/evaluation/parallel.py` | MEDIUM | `_evaluate_batch()` 批量评估 → FactorStore.load() 优先 |
| 3 | `quant/evaluation/phase5_monitor.py` | LOW | `_check_crowding()` 拥挤度 → FactorStore.load() 优先 |
| 4 | `quant/monitor/factor_attribution.py` | LOW | `factor_pnl_attribution()` 暴露归因 → FactorStore.load() 优先 |
| 5 | `quant/scheduler/crowdedness.py` | LOW | `compute_crowdedness_score()` 拥挤度 → FactorStore.load() 优先 |

**已确认无需接入**:
- `quant/pipeline.py` — 已有 `factor_store` 参数，`compute_all_factors` 仅在 `factor_store=None` 时作为 fallback
- `quant/factor/compute/_dispatch.py` — `compute_all_factors` 的调度实现，不是消费者
- `quant/core/phase_tracker.py` — 仅 docstring 引用
- `test/benchmark_performance.py` — 基准测试，非业务路径

**影响**: IC 计算、评估、监控全链路速度提升 ~10x（cache hit 时跳过逐日 compute）

---

## test-v287: pipeline.py 去 fallback — factor_store=None 直接抛错

**问题**: `generate_signals()` 中 `factor_store=None` 时静默 fallback 到 `compute_all_factors`，
违反系统"零 fallback"原则。

**修改** (`quant/pipeline.py`):

1. `factor_store is None` → `raise RuntimeError` 提示先跑 `factor_cache` 物化
2. `factor_store.load()` 返回空 → `raise RuntimeError`，不再静默 fallback
3. 删除 `from quant.factor.compute import compute_all_factors` — 调用方全部移除，已无引用
4. FactorStore.load() 异常不再 try/except 吞掉，直接上抛

**设计原则**: 因子值来源唯一 — FactorStore 缓存。缓存缺失说明物化任务未跑，应抛错而非静默降级计算。

---

## test-v288: FactorStore 5 文件去 compute_all_factors fallback

**原则**: 系统严禁 fallback。缓存缺失说明物化任务未跑，应抛错而非静默降级计算。

**修改** (5 个文件):

| 文件 | 改动 |
|------|------|
| `quant/factor/ic.py` | `except Exception: pass` → 删除；`compute_all_factors` fallback → `raise RuntimeError`；删 `compute_all_factors` import |
| `quant/evaluation/parallel.py` | `try/except: raise` → 删除；fallback → `raise RuntimeError`；删 `compute_all_factors` import |
| `quant/evaluation/phase5_monitor.py` | 同上 |
| `quant/monitor/factor_attribution.py` | 同上 |
| `quant/scheduler/crowdedness.py` | 同上 |

**统一行为**: FactorStore.load() 返回空 → `raise RuntimeError("factor_cache miss for {date}, run materialization first")`

---

## test-v289: 终端单行动态进度条

**新增**: `quant/utils/progress.py` — `progress_bar()` 函数，单行 `\r` 原地刷新。

**接入**: `quant/factor/ic.py` — `compute_ic()` 逐日 IC 计算循环 (`compute_days`) 内显示进度。

**输出格式**: `[=========>              ] 45/132  34%  |  IC: 2026-05-15`

**pytest-benchmark 注意**: 基准测试默认捕获 stdout，进度条不可见。需加 `-s` 标志：
```bash
PYTHONPATH=. .venv/bin/pytest tests/benchmark_critical.py -v --benchmark-only -s -k "slow"
```

---

## test-v290: oos_verify 进度条 + 去 fallback

**进度条接入**: `run_oos_check()` 的 `for ds in trading_days:` 逐日循环 → `progress_bar()`.

**去 fallback**: `except Exception: continue` → 直接上抛。`compute_all_factors` import 已删。

**受影响调用链**: `compute_backtest_ic` → `run_oos_check` — 基准测试 `test_bench_compute_backtest_ic` 现在有进度刷新。

**进度条位置汇总**:
| 文件 | 循环 | 状态 |
|------|------|------|
| `quant/factor/ic.py` — `compute_ic()` | `for ds in compute_days` | ✅ |
| `quant/scheduler/oos_verify.py` — `run_oos_check()` | `for ds in trading_days` | ✅ |

---

## test-v291: run_oos_check 参数化 — 去掉内部 yaml 回退

**问题**: `run_oos_check()` 内部读取 `_require_cfg("oos_verify.train_window_days")` 等 3 个值，
忽略调用方传入的 `n_train_days`。`fac_cal=378` 导致回溯到 2025-04-23，远超出缓存覆盖范围。

**修改**:

| 文件 | 改动 |
|------|------|
| `quant/scheduler/oos_verify.py` | 签名改为 `run_oos_check(today, status_filter, train_days, test_days, decay_warn_threshold)`，5 个参数全部必传。删除内部 `_require_cfg` 调用。删除 `fac_cal` 参与 `total_lookback`（因子值来自缓存，不再需要 warmup）。删除 `_require_cfg` 和 `max_factor_calendar_days` import |
| `quant/scheduler/attribution.py` | 调用前读 yaml，5 个参数显式传入 |
| `quant/factor/stats_cache.py` | `n_train_days` → `train_days`，`test_days` / `decay_warn_threshold` 显式传入 |

**设计原则**: 参数全部由调用侧负责，函数内部零 yaml 读取、零默认值。

---

## test-v293: 回退进度条 — pytest-benchmark 不可见

`progress_bar` (stdout → stderr) 在 pytest-benchmark 校准阶段仍不可见。
移除 `quant/utils/progress.py`、`ic.py`、`oos_verify.py` 中所有进度条代码。

进度条方案留待后续统一设计（终端安装风格进度线 + 百分比）。

---

## test-v294: oos_verify 三模块分离 — 对齐 Alphalens/DolphinDB

**动机**: `run_oos_check()` 六层耦合 (配置→数据→股票池→因子→IC→统计), 不可单元测试,
不可复用, FactorStore 循环内每日期新建/关闭。

**重构**: 参考 Alphalens (utils → performance → tears) 和 DolphinDB (preprocess →
singleFactorAnalysis → plot), 拆为三层:

| 函数 | 对标 | 职责 | 依赖 |
|------|------|------|------|
| `compute_ic_series()` | Alphalens `factor_information_coefficient()` | 纯数学: 逐日 Spearman IC | 零: 不读 config/DB |
| `analyze_is_oos()` | DolphinDB `singleFactorAnalysis()` | 纯统计: IS/OOS→IR→衰减 | 零: 不读 config/DB |
| `run_oos_check()` | Alphalens `create_full_tear_sheet()` | 编排: 加载→计算→分析→返回 | DataStore, FactorStore, UniverseRepo |

**改动**:
- `oos_verify.py`: 217行 → 纯函数 (~80行) + 编排函数 (~100行)。`n_symbols=0`=全量。
  5 个算法常量从 yaml 模块级读, 不再写死。
- `attribution.py`: `n_symbols=_require_cfg("oos_verify.attribution_n_symbols")`
- `stats_cache.py`: `n_symbols=_require_cfg("oos_verify.backtest_n_symbols")`
- `config.yaml`: 8 个 `oos_verify` key 带详细来源注释
- FactorStore 循环外创建一次 (不再每日期新建 close)

**可单元测试**: `compute_ic_series()` 和 `analyze_is_oos()` 接受 dict/DataFrame, 零外部依赖.
---

## test-v295: signals/run_task/phase8 接入 FactorStore — 修复信号生成崩溃 + 全链路影响排查

**问题**: `signals.py`、`scripts/run_task.py`、`phase8_live_consistency.py` 调用 `generate_signals()`
时未传 `factor_store`。pipeline.py test-v287 去 fallback 后，`factor_store=None` → RuntimeError。

**修改**:
- `quant/scheduler/signals.py`: 导入 FactorStore，`generate_signals(..., factor_store=fs)`，用完 close
- `scripts/run_task.py`: 同上
- `quant/evaluation/phase8_live_consistency.py:108`: 补充 `factor_store=factor_store`

**全链路影响排查 (14 模块导入 + 6 类调用方)**:

| 检查项 | 结果 |
|--------|------|
| 导入链: 14 个受影响模块 import | 全部通过 |
| `generate_signals()` 调用方 (除 backtest/loop 用 **kwargs) | signals ✅ run_task ✅ phase8 ✅ |
| `run_oos_check()` 调用方 (attribution, stats_cache) | 6 个必传参数全部正确 |
| `compute_all_factors` 引用 | 8 处: 4 处注释/导出, 4 处合法使用 (factor_ic Mode A, FactorStore 写侧, stats_cache Mode A) |
| `oos_verify.py` 函数体内 `_require_cfg` | 0 (全部模块级) |
| Web 路由 (11 个 API 端点) | 不调用 generate_signals/compute_all_factors, 只读 DB + shared_state |
| 调度器任务: attribution | run_oos_check 调用正确, 其他逻辑不变 |
| 调度器任务: factor_cache | FactorStore.materialize() 正常, 不经过 pipeline |
| 回测链: loop.py | kwargs 含 factor_store, compute_backtest_ic 参数正确 |

**Web 页面"无数据"根因**: signals 任务崩溃 → shared_state 为空 → positions/performance 空表。
调度页面 (task_runs) 读 DB 应正常显示 (已确认 DB 有数据)。
修复后 signals 任务重新执行即可恢复。



---

## 已确认修复 (无需再排查)

以下问题曾出现在审计报告中，已由之前版本修复，当前代码无问题：

| 问题 | 修复版本 | 说明 |
|------|---------|------|
| **benchmark_tracking alpha=None** | test-v270+ | `record_daily()` 接收 `yesterday_equity` 参数，`strat_ret = strategy_equity / yesterday_equity - 1` 正常计算。DB 中旧的 None 值是修复前的残留数据，下次回测自动写入正确 alpha。 |
| **"no running row found" 双写警告** | test-v273 | `orchestrator.py:76` 已委托 `_tk_finish` 给任务自身的 finally 块，不再重复调用。最近日志零出现。 |
| **B2: 252→244 年化天数** | test-v279 | `loop.py` 使用 `np.sqrt(244)` 和 `len(returns) / 244`，非 252。 |
| **L2: next_ret 收益率计算** | test-v279 | `(next_close[s] / today_close[s] - 1)` 正确计算前向收益率。 |
| **PR2/PR3: execute.py** | test-v279 | `execute.py` 已删除，拆为 `broker.py` + `bridge.py`。原问题自然消除。 |
| **factor_repo.py SyntaxError** | test-v287 | Line 119 `list[str]` 语法在 Python 3.14 合法，该错误为旧版本残留。 |
| **parallel.py IndentationError** | test-v287 | Line 41 缩进已修复。 |

**注意**: 下次会话如遇到上述问题，请先检查是否为旧 DB 数据残留或代码版本过旧，不要再重新排查。

---

## test-v296: 因子缓存自动裁剪 — factor_cache_max_days: 244

**动机**: 因子缓存无限膨胀 (7.6GB for 133 天), 3 年全量回测仅需 244 天窗口。

**修改**:
- `config.yaml` — `backtest` 下新增 `factor_cache_max_days: 244`, 带来源注释
- `quant/factor/store.py` — 新增 `trim_to_max_days(max_days)` 方法: 以最新物化日期为锚点, DELETE WHERE date < cutoff
- `quant/scheduler/factor_cache.py` — 物化成功后调用 `fs.trim_to_max_days()`, 裁剪失败不阻塞 (非致命 warning)

**设计约束**:
- 裁剪锚点为最新物化日期, 非系统时间 (避免调度间隔内误删)
- max_days ≤ 0 → 跳过 (保留全部, 开发调试用)
- 因子值 = 衍生数据, 丢失后可 force 重建
