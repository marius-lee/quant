# HANDOFF — 2026-07-16 (test-v110)

## test-v110: 冒烟测试 capital=-985 修复 — 回测旧交易记录污染 get_cash()

**根因**: `backtest_trades.db` 中保留了 5087 条旧回测交易（之前的 `smoke_1` 运行）。
`run_backtest()` 只调了 `set_initial_capital()` 覆盖 `strategy_config.initial_capital=5000`，
但 `sim_trades` 的旧买入/卖出记录未清理。`get_cash()` 计算公式：

```
cash = initial_capital(5000) + SUM(sells, 245318.9) - SUM(buys, 251303.9) = -985
```

负数资本传入 `PortfolioConstructor.construct()` → `_equal_weight_greedy()` 无法分配任何 lot → ValueError.

**修复**: `run_backtest()` 在 `set_initial_capital()` 后、首次 `get_capital()` 前，
用 `sqlite3.connect(BACKTEST_DB)` 直接 `DELETE FROM sim_trades WHERE strategy=?` 清理旧记录。

**影响**: 每次回测启动都是干净的资金状态，不再被旧交易数据污染。

### 版本: test-v110

---

# HANDOFF — 2026-07-16 (test-v109)

## test-v109: 诊断数据源修复 — save_phase("diagnostics") 归属调整

**根因**:
- `loop.py` 的 `run_backtest()` 每次回测结束都调 `save_phase("diagnostics")`，冒烟测试（仅 2 个 active 因子）的诊断数据覆盖了 `evaluation_runs`
- `run_diagnostics.py`（独立诊断模块，66 个 backtesting 因子）只打日志不入库
- Phase 2 `load_latest("diagnostics")` 从 DB 读到的是冒烟测试的 2 因子数据，与 66 个 backtesting 因子取交集为空

**设计原则**:
- 冒烟测试 = 验证管线不崩，诊断结果不入库（无意义）
- 独立诊断模块 = 对 backtesting 因子做 IC 快照，诊断结果**必须入库**（供 Phase 2 预筛）
- Phase 2 = 取诊断 passed 因子 ∩ backtesting 因子做预筛

**改动**:

1. **`quant/backtest/loop.py`** — 删除 `save_phase("diagnostics")` 块 (原 L359-374)
   - 回测（含冒烟测试）诊断结果仅内部使用（`apply_diagnosis` 调整 IC 权重 + 返回 `diagnosis` 字段）
   - 不再写入 `evaluation_runs`

2. **`scripts/run_diagnostics.py`** — 新增 `save_phase("diagnostics")` 入库
   - IC 计算完成后，将 passed（|IC|>=0.02 的因子）和 factor_report 写入 `evaluation_runs`
   - `backtest_strategy="diagnostics"` 标记来源

3. **`quant/evaluation/phase2_single.py`** — 更新注释和日志
   - 注释说明诊断数据来源是独立诊断模块
   - 日志提示用户先跑 `scripts/run_diagnostics.py`
   - 安全网保留：交集为空时退回全部 backtesting 因子

**影响**: Phase 2 的 `prefilter_from_diagnostics=True` 现在正确读取独立诊断模块的数据，而不是冒烟测试残留。

### 版本: test-v109

---

# HANDOFF — 2026-07-16 下午 (test-v102 ~ v106)

## test-v106: cash_balance 冗余列删除 + sim_trades 成为资金唯一真相源

**根因**: `strategy_config.cash_balance` 是 sim_trades 的冗余缓存。删 sim_trades 时 cash_balance 不同步 → 资金显示 -10,846 但交易记录为 0。

**修复**:
- `trade_repo.py`: `get_cash()` 改为 `initial_capital + SUM(sells) - SUM(buys)` 实时计算
- `trade_repo.py`: `record_trade()` 删除 cash_balance UPDATE，只写 sim_trades
- `trade_repo.py`: `_ensure_tables()` CREATE TABLE 移除 cash_balance 列 + 迁移删除旧表该列
- `trade_repo.py`: `set_initial_capital()` INSERT 不再写 cash_balance
- `trade_repo.py`: sim_trades 新增 `cost` 列（存储佣金+印花税+滑点）
- `engine.py` / `cost.py`: 更新注释

**原则**: 资金的唯一真相源是 sim_trades。任何缓存都不可靠。

## test-v104: 重复挂单 + 负资金防护

**根因**: monitor 修复后开始干活，但之前每次重启产生的 26 条重复挂单被逐一成交 → 16 笔买入 → 资金负数。

**修复**:
- `execute.py`: 挂单前 `cancel_all(today)` 清旧单防重启重复
- `order_manager.py`: `_fill()` 先 `get_cash()` 检查资金，不够则 cancel 不成交

## test-v103: unrealized_pnl 无持仓时为 0

**根因**: `unrealized = total_pnl - realized` 在有交易费用时，即使无持仓也算出负数。
**修复**: `state_broker.py` 两处: `pos_value == 0` → `unrealized = 0`

## test-v102: monitor 限价单管理独立于持仓

**根因**: `monitor.py` 第 82 行 `if positions:` 把行情拉取和 `check_and_manage` 全包在里面。无持仓时永
远不拉行情 → 挂单永远不成交。

**修复**: 订单管理提取到 `if positions:` 之外，合并持仓符号+挂单符号统一拉行情，每 5s 运行。

**原则**: 订单管理（执行层）与止盈止损（风控层）分离。

---

# HANDOFF — 2026-07-15 ~23:30 CST

## 当前状态：backtesting 筛选统一 + 诊断写因子状态 bug 修复 (test-v85)

### #49: 日志系统全面修复 (13 files)
- JSON 统一格式、stderr 过滤、propagate=False、双前缀修复、trace_id 提前
- trace_id 不重复设置 (pipeline.py: get_trace_id() or new)

### #50: 冒烟测试 — 因子范围修正
- `factor_status_filter="backtesting"` → `"active"` (66→2 因子)
- 耗时从数十分钟回到 151.6s
- **注意**: 冒烟测试用 active，诊断模块必须用 backtesting——两者职责不同

### #51: IC 计算提速 — primitives 延伸

**根因**: `compute_ic()` 虽然一次性加载了 data，但没调 `precompute_primitives`，导致每个交易日 `compute_all_factors` 都从原始行情重算 sma_20/vol_20/ret_5d 等滚动窗口。66 因子 × 60 天 = 3960 次重复计算。

`run_backtest()` → `generate_signals()` 链路早就在用 primitives，只是 `compute_ic()` 忘了传。

**修复**: `quant/factor/ic.py` — 3 处改动:
1. 加载 data 后调 `precompute_primitives(data)` 一次
2. `_compute_one_day` 内按日期切片 primitives
3. 切片后的 prims 传给 `compute_all_factors(primitives=ds_prims)`

效果: IC 计算阶段提速 ~3-5x（12 个 shortcut 因子走 O(1) 快捷路径，其余因子也复用滚动窗口中间值）。

### 版本: test-v84

### #53: 僵尸任务三层防护 (test-v84)

**修复内容**:
1. `execute.py` 末尾补 `_tk_finish("ok")` — 之前只打日志不写 DB
2. `task_log.start()` 插入前自动 abort 同任务同日期旧 running 行
3. `orchestrator.py` 新增 `_check_timeouts()` — 每轮 poll 扫描超时 running, 任务专属阈值
4. `api_scheduler()` 超时显示同步改为任务专属阈值

**各任务超时阈值**:
| 任务 | 阈值 | 正常耗时 |
|------|:---:|------|
| signals | 15min | ~5min |
| execute | 10min | <1min |
| monitor | 不收市不检查 | 09:35-14:55 |
| attribution | 15min | ~3min |
| weekly_eval | 120min | ~30min |

**待归档: heartbeat 方案** — `task_runs` 加 `last_heartbeat` 列, 长任务关键循环点心跳更新, watchdog 查 DB 替代固定阈值. 当前固定阈值方案够用, heartbeat 留待将来.

### #54: Phase 2 primitives 预计算优化 (test-v84)

**问题**: `compute_factor_stats()` 的 `_thread_compute_chunk` 逐因子逐日调用 `compute_all_factors()`, primitives 重复计算 N 次
(与回测诊断 4.7h→8min 同根问题).

**方案**: 主线程统一预计算 primitives, 线程 worker 复用.
- `compute_factor_stats` Phase B 之前新增 primitives 预计算块
- `_thread_compute_chunk` 接受 `shared_data` + `shared_primitives` + `shared_financials` 参数
- worker 内调用 `compute_all_factors(data, date_str, precomputed_primitives=prims)`
- 去掉 worker 里独立打开 DataStore / 重复加载数据的逻辑

**改动文件**: `quant/factor/stats_cache.py`
**影响**: Phase 2 单因子 IC 评估耗时预计降低 60-80%

### #55: backtesting 筛选条件统一 + 诊断模块写入 factor_registry 修复 (test-v85)

**Bug 1 — backtesting 筛选不一致**:
- `_registry.py`: `backtesting` → `('registered', 'candidate', 'retired')`
- `stats_cache.py:_load_ic_from_db`: `backtesting` → 包含 `'active', 'monitoring'`（多余）
- 回测时因子计算只用 3 状态，但 IC 权重加载了 5 状态 → 权重被稀释

**修复**: `stats_cache.py:510-511` 删除 `'active', 'monitoring'`，统一为 3 状态。

**Bug 2 — 诊断模块内 auto-retire 污染因子状态**:
- `loop.py:296-305` 在诊断完成后自动把 `recommendation="drop"` 的因子设为 `retired`
- `loop.py:342-355` 直接写 `status_reason` 到 factor_registry
- 两步架构的职责边界：Step 1 诊断仅出报告（写 evaluation_runs），Step 2 评估管线 sync_factor_status 统一改状态
- 第一次诊断（#51 前 IC 计算 broken）将所有因子标为 drop → 全部 auto-retired → 68 个 retired

**修复**: 删除 `loop.py` 中的 auto-retire 块和 status_reason SQL 写入。诊断结果仅通过已有的 `save_phase("diagnostics")` 写入 `evaluation_runs`。

**回滚 — 68 个 retired 因子状态恢复**:
| 目标状态 | 数量 | 依据 |
|:------|:---:|------|
| `candidate` | 11 | notes 含"激活"或"启用"（曾通过评估） |
| `registered` | 55 | notes 含"失效"、空白或公式描述 |
| `rejected` | 2 | northbound_20d, northbound_streak（数据源永久失效） |

北向资金因子特殊处理: `rejected` 而非 `retired`，原因="数据源停止提供(证监会不再披露北向资金)"。retired 暗示将来可复用，不适合此场景。

### 版本: test-v85

### test-v86: contextvars 离线日志路由 + 缩进统一

**背景**: 回测/诊断/评估三个离线入口使用共享模块（pipeline/factor/risk），日志会同时写 app.log 和 backtest.log，造成日志混淆和丢行问题。

**contextvars 方案 — `quant/utils/logger.py`**:
- 新增 `_offline_mode: ContextVar[bool]` — 标记当前上下文是离线模式
- 新增 `offline_mode()` context manager — 进入时设 True，退出时恢复
- `_is_backtest()` filter 检查 `_offline_mode.get()` — True 时日志路由到 backtest.log

**三个入口覆盖**:
| 入口 | 方式 | 状态 |
|------|------|:--:|
| 回测 `run_backtest()` | `loop.py:110` 直接包装 `with offline_mode():` | ✓ |
| 冒烟测试 `smoke_test.py` | 调用 `run_backtest()` 间接覆盖 | ✓ |
| 诊断 `diagnostics_test.py` | 调用 `run_backtest()` 间接覆盖 | ✓ |
| 评估 `eval_standard.sh` | 7 个 phase 各自 `python3 -c` 内 import+with | ✓ |

**修复 — `eval_standard.sh` Phase 2 bug**:
- Phase 2 的 `from quant.utils.logger import offline_mode` 和 `with offline_mode():` 泄漏到 bash 层
- 修复: PREFILTER 逻辑提前到 python3 -c 之前，import 和 with 放入 python 字符串内
- 同时验证全部 7 个 phase 均为 import 在前、with 在后

**缩进统一 — 3 文件 27 处**:
- `quant/utils/logger.py`: L4-L8 docstring 2空格→4空格
- `quant/backtest/loop.py`: L94/L277-278/L324/L342-349 续行缩进→4的倍数
- `quant/factor/stats_cache.py`: L14-15 docstring + L156/L171-174/L205/L280/L288/L325/L333 续行→4的倍数

### test-v96: 回测专业分析 P0-P2 全部落地

**背景**: 回测逻辑专业分析发现 6 个问题，按优先级全部落地。

**P0-1 — UniverseRepo.get_symbols() 生存偏差修复**:
- `universe_repo.py`: `get_symbols()` 接受 `start_date`/`end_date` 参数
- JOIN stocks 时过滤 `list_date <= end_date`（排除未来IPO股票）
- 过滤 `delist_date > start_date`（保留已退市但期间活跃的股票）
- 消除 survivorship bias 和 look-ahead bias

**P0-2 — 删除 bt_engine.py**:
- `bt_engine.py` 已删除（`_extract_equity_curve` 存在致命bug: cerebro.broker.getvalue() 循环内始终返回终值）
- `__init__.py` 移除 `run_backtest_bt` 导入
- `run_backtest_bt` 仅内部引用，无外部调用者，安全删除
- 双引擎分歧问题一并解决

**P1-1 — 回测指标增强**:
- `_compute_backtest_metrics()` 新增 5 个指标:
  - **Sortino**: annualized, 仅惩罚下行波动（更适合A股高波动特征）
  - **Calmar**: CAGR / |MDD|
  - **Alpha**: 年化超额收益（vs 沪深300基准）
  - **Info Ratio**: 超额收益 / 跟踪误差
  - **Beta**: 市场暴露系数
- 函数签名改为 `_compute_backtest_metrics(equity_curve, benchmark_returns=None)`
- `run_backtest()` 在 store.close() 前拉取沪深300基准数据，传入指标计算
- 当 benchmark_returns 为空或样本不足时，Alpha/IR/Beta 返回 None（不报错）

**P1-3 — pytdx 复权已验证**:
- 四个数据源均已提供 qfq 前复权价格:
  - akshare: `adjust="qfq"`（store.py:545/751）
  - tencent: qfq（store.py:910 注释确认）
  - pytdx: 手动 qfq 计算（store.py:651-720）
  - sina: 已移除（未复权）
- market.db 数据已全面复权，无需额外修改

### 版本: test-v96

### test-v97: 每日数据拉取自动化 + 调度时序调整

**背景**: 数据拉取从未自动化，market.db 停留在手动拉取的最后日期。
数据源（东方财富/腾讯/通达信）收盘后 30-60 分钟才更新，15:30 拉取不可靠。

**改动**:

1. **新建 `quant/scheduler/daily_data.py`** — 每日 19:00 数据拉取调度器，调用 `DataStore().update_daily()`
2. **`scripts/run_task.sh`** — 新增 `daily_data` 路由
3. **`scripts/setup_cron.sh`** — 新增 `0 19 * * 1-5 daily_data`，attribution 从 `30 15` 改为 `0 20`
4. **`quant/scheduler/orchestrator.py`**:
   - 注册 `daily_data` 任务（19:00）
   - `done` 字典新增 `daily_data` 键
   - 新增 19:00 数据拉取执行块
   - attribution 从 15:30 改为 20:00，增加 `done["daily_data"]` 前置依赖
   - `_TIMEOUTS` 新增 `"daily_data": 1800`（30min）

**最终调度时序**:
```
周一~五:
  08:30  signals      信号生成（依赖前一天数据）
  09:30  execute      交易执行
  09:35  monitor      盘中风控
  19:00  daily_data   拉取当日收盘数据
  20:00  attribution  盘后归因（依赖 daily_data 完成）
周六 06:00  weekly      周频因子评估
```

### 版本: test-v97

---

### test-v111: 因子回测与策略回测分离 — FactorStore 架构落地

**设计原则**:
- 三个独立操作: 因子物化 → 因子回测 / 策略回测 (两个互相独立)
- 共享数据底座: `factor_cache.db` (独立于 market.db)
- 策略调参不触发因子重算

**新增文件**:
- `quant/factor/store.py` — FactorStore: materialize() + load() + is_materialized()
- `scripts/materialize_factors.py` — CLI 工具

**修改文件**:
- `quant/pipeline.py` — generate_signals() 加 factor_store 参数 (缓存优先, 向后兼容)
- `quant/backtest/loop.py` — run_backtest() 加 factor_store 参数, 透传给 generate_signals()
- `docs/proposals/factor-strategy-separation-plan.md` — v2 修订版

**使用**:
```bash
# 1. 物化因子值
PYTHONPATH=. .venv/bin/python scripts/materialize_factors.py
# 2. 跑回测 (自动读缓存)
PYTHONPATH=. .venv/bin/python scripts/smoke_test.py
```

### 版本: test-v111

---

### test-v112: 原方案逐项落地 — analyze/bridge/gate 全线实施

**P0 — analyze.py: 策略层诊断独立**:
- 新建 `quant/backtest/analyze.py` — FactorTracker / diagnose / apply_diagnosis
- `diagnostics.py` 精简为因子层专用 — 只保留 compute_pre_backtest_ic()
- `loop.py` 导入更新: analyze.py 替代 diagnostics.py 中的策略层函数
- 对照: Quantopian Pyfolio (独立于 Zipline 的 post-backtest 分析)

**P1 — DSR/PBO 硬门禁**:
- `phase3_oos.py`: PBO 未通过 → raise ValueError (fail-fast, 零 fallback)
- `phase6_backtest.py`: 运行前检查 Phase 3 gate, 未通过 → raise ValueError
- 门禁标准: PBO < pbo_max (config), kept > 0

**P2 — 评估→回测桥接**:
- 新建 `quant/backtest/bridge.py` — evaluation_to_backtest()
  - 读取 `evaluation_runs` 的 Phase 2/3 结果
  - 返回 (factor_names, ic_map)
  - 评估未运行 → raise ValueError (fail-fast)
- `materialize_factors.py` 新增 `--from-evaluation` 标志
  - 自动从评估结果读取因子名, 消除手动配置

**设计原则**:
- 所有门禁均为 fail-fast (raise), 零 fallback
- 桥接走 DB (evaluation_runs), 不依赖临时文件
- 因子层 (diagnostics.py) 和策略层 (analyze.py) 物理分离

### 版本: test-v112

---

### test-v113: eval_standard.sh Phase 2 全部 IC=0 修复 — 符号集统一

**Bug**: `eval_standard.sh` 的 Phase 2 全部 66 个因子 IC=0。根因是 `stats_cache.py` 的两套符号逻辑冲突：
- `eval_date_strs` 从 `SELECT DISTINCT date FROM daily` 查询**全量**符号日期
- `_shared_data` 从 `store.get_daily()` 加载**top-N** 符号数据
- top-N 符号缺少某些日期 → `compute_all_factors` → KeyError，静默跳过 → IC 全部为 0

**修复**:
1. `stats_cache.py` 行 65-68: 替换内联 SQL 为 `UniverseRepo.get_symbols()` (与 loop.py / diagnostics.py 统一)
2. `stats_cache.py` 行 130-137: `_shared_data` 加载后过滤 `eval_date_strs` 到实际数据日期，丢弃不存在的日期并打 WARNING

**设计原则**:
- 符号集统一用 `UniverseRepo`，消除与 `loop.py` 的分歧
- 数据不完整时明确 WARNING，而非静默跳过导致 IC=0

**验证**: `PYTHONPATH=. bash scripts/eval_standard.sh`

### 版本: test-v113

---

### test-v114: `_analyze_daily_gaps` 近期数据缺失检测修复

**Bug**: daily_data 任务只拉取 250 天未更新的僵尸股票，忽略 07-14/15/16 三个交易日的全量数据缺失。
根因是 `_analyze_daily_gaps` 只有 `missing` + `stale(>250d)` 两级分类，DB 最新日期 07-13 落后于最近交易日 07-16，但所有 5475 只股票被归类为 `full`。

**修复**:
1. `_analyze_daily_gaps` 新增 `stale_recent` 分类: 当 `global_max(DB) < most_recent_td(日历)` 时，所有有数据的股票自动归入 `stale_recent`，纳入拉取列表
2. `update_daily` 的 `target` 列表包含 `stale_recent`
3. 检测逻辑: 先取 `SELECT MAX(date) FROM daily` 得全局最新日期，再取交易日历最近交易日，两者比较

**设计原则**:
- 两层检测互不干扰: 长期 stale (>250d) 和近期 stale (DB 落后日历) 独立判定
- per-symbol batch_start_map 已处理增量: 已有数据的股票只拉缺失日期，INSERT OR IGNORE 防重复

**待验证**: 数据源连通性 (tencent/akshare 当前网络环境不可达)

### 版本: test-v114

---

### test-v115: tushare 限频保护 — token 无效时从 source 列表移除

**Bug**: daily_data 任务无条件将 tushare 列入 source 列表，即使 token 未配置也会触发 API 调用，5000+ 股票拉取时迅速打爆 50次/分钟限额。

**修复**:
- `update_daily` 的 `all_sources` 列表移除了 tushare 常驻项
- tushare 仅在 `pro is not None`（token 有效）时通过 `insert(1, ...)` 插入第二顺位
- 默认 source 顺序: tencent → akshare → pytdx（tushare 按 token 状态动态插入）

**设计原则**:
- 无 token 时不触碰 tushare API，避免无效调用浪费限频配额
- 有 token 时仍保留 tushare 作为优选备源（第二顺位，tencent 失败后首选）

### 版本: test-v115

---

### test-v116: FactorStore.materialize 缺少 ztd cache 预加载修复

**Bug**: `materialize_factors.py` 崩溃 — `compute_ztd()` 调用前 `preload_ztd_cache()` 未执行。
`ztd` / `zt_streak` 等涨跌停因子需要在计算前预加载停牌/涨跌停缓存。

**修复**:
- `quant/factor/store.py`: 在 `materialize()` 的因子计算循环前新增 `preload_ztd_cache(date_range, symbols)` 调用
- 添加 import: `from quant.factor.compute.price._alternative import preload_ztd_cache`

### 版本: test-v116

---

### test-v117: 符号集统一 + benchmark dtype 防护

**Bug 1**: `materialize_factors.py` 用 `ORDER BY symbol` 选股（字母序前800），`smoke_test.py`/`loop.py` 用 `UniverseRepo`（流动市值排名）。两套符号集几乎不重叠，导致 `factor_store.load()` 返回空数据 → 8天全部 empty common universe。

**Bug 2**: 空仓时 equity_curve 平坦，benchmark 数据也为空，返回 int64 index 的 Series，与 returns 的 datetime64 不兼容，`reindex` 崩溃。

**修复**:
- `scripts/materialize_factors.py`: 股票池改用 `UniverseRepo().get_symbols()`，与所有回测代码统一
- `quant/backtest/loop.py`: `_compute_backtest_metrics` 增加 dtype 检查，index 类型不一致时跳过 benchmark 计算而非崩溃

### 版本: test-v117


### 版本: test-v118 — excepthook 双重日志 + _compute_backtest_metrics 崩溃保护

**日期**: 2026-07-16

**修改原因**:
两处崩溃相关问题:
1. 回测崩溃日志同时出现在 app.log 和 backtest.log — excepthook 先路由到 backtest.log，再调用原始 hook 重复写进 app.log
2. `_compute_backtest_metrics` 缺乏形状检查和变量初始化防御

**修改内容**:

1. `quant/utils/excepthook.py` (line 33-44):
   - 在 _hook() 中对回测上下文 (`_is_bt`) 直接 return，不再调用 `_original(exc_type, exc_value, exc_tb)`
   - 原理: `_original` 是 logger._init() 安装的 `_log_uncaught`，它会写 root.critical("未捕获异常:...") →
     app.log。回测崩溃现已由 quant.backtest.crash logger 正确写入 backtest.log，不重复

2. `quant/backtest/loop.py` — `_compute_backtest_metrics()` (line 96-122):
   - `alpha/ir/beta` 初始化前置于 if 块外
   - `bm_returns.empty` 检查 → 无数据时跳过
   - `len(strat) <= 1 or len(bm) <= 1` → 样本不足时跳过协方差计算
   - `cov_mat.shape == (2, 2)` → 防止 np.cov 返回意外形状导致 IndexError
   - `beta_val = 0.0` 初始化 → 屏蔽 UnboundLocalError（beta_var ≤ 0 时）
   - except 增加 `IndexError` → 捕获形状异常

**500万日成交额 (min_daily_amount) 来源说明**:
- 定义位置: `quant/config/config.yaml:205` / `quant/risk/constraints.py:19`
- 来源: 开发者实际盘口观测（无业界文献）
- 业界标准对照: config 中另有 `min_daily_turnover_amount: 30000000` (line 232)，这是 A 股小票流动性实际门槛
- 判定: 500 万不是专业标准，用于回测(含回测新近2000多只小票)没问题，实盘策略建议调至 3000 万

**注意事项**:
- 回测崩溃现在只写入 backtest.log，不再产生 app.log 重复条目
- 回测入口 (smoke_test.py, run_diagnostics.py, eval_standard.sh) 必须调用 excepthook.setup() 才能生效


### 版本: test-v119 — Pipeline 对齐业界标准流程 (风险预过滤前置)

**日期**: 2026-07-16

**修改原因**:
冒烟测试 Day 2-10 零成交的根本原因已定位: pipeline 在非 IC 重训日将数量有限的股票 (22-34 只)
送入 `apply_all_filters`，这些股票因周转率数据或其他原因全部被过滤掉。这源于旧流程中风险
过滤作用在 alpha 子集上，而非业界标准的「全 universe 预过滤」模型。

**业界标准流程**:
  全市场 (5000+)
    ↓
  风险预过滤 (流动性/ST/股价) → 可投资域 (2000-4000)
    ↓
  因子计算 ← 对整个可投资域
    ↓
  Alpha 模型评分 ← 对整个可投资域
    ↓
  组合优化 → 最终持仓

**修改内容**:

1. `quant/pipeline.py` — 新增 Step 2.3 (风险预过滤):
   - 在 Step 2 (load) 之后、Step 2.5 (turnover rank) 之前插入
   - 对全量 load data 构建 `{"close": ..., "amount": ...}` DataFrame
   - 调用 `apply_all_filters(limits=RiskLimits.from_config(), stock_names=...)`
   - 把 `symbols` 缩小到「可投资域」= 全 universe 与通过过滤的股票的交集
   - 日志 `[2.3] risk pre-filters: 5208 → {N} investable`

2. `quant/pipeline.py` — Step 4 (risk) 简化为:
   - 删除 `apply_all_filters(candidates.reindex(prices.index))`
   - 改为 `candidates.dropna(subset=["alpha"])` — 仅丢弃 alpha 无分的股票
   - 逻辑: 风险过滤已在 Step 2.3 完成，Step 4 只负责中性化 + 协方差

3. `quant/pipeline.py` — import 清理:
   - 顶部保留 `from quant.risk.constraints import RiskLimits, apply_all_filters`
   - 删除函数内重复导入

**效果**:
- 无论 IC 重训与否，管线每天都在 2000+ 的可投资域上计算因子和 alpha
- 不再出现 22-34 → 0 的全量过滤
- 中性化和协方差在已过滤的可投资域上运行，不依赖 `reindex(prices.index)`

---

## test-v120 — 因子函数数据加载规范化 (2026-07-17)

**问题**: 9个因子函数各自用 `sqlite3` 直连 `market.db`，与 `preload_aux_data` 的批量预加载机制不一致。
`_dispatch.py` 在计算前已预加载10张辅助表到 `aux` dict，但价格因子无视它：
- 3个签名有 `aux=None` 但函数体不检查（`compute_lhb_net_buy`, `compute_main_flow_ratio`, `compute_analyst_buy`）
- 4个连 `aux` 签名都没有，各自 `_db_connect()`（`compute_lhb_post_quality`, `compute_margin_balance_chg`, `compute_margin_buy_ratio_price`, `compute_fund_change`）
- `_dispatch.py` 用 `except TypeError` 兜底——被修改为 `inspect.signature` 显式检查

只有 2 个基本面因子的 `aux` 使用模式正确。

**对齐的业界标准**: Qlib/LEAN/RQAlpha — 因子函数不应知道数据存在哪里，所有数据通过参数传入。

**修改**:

### 1. `_preload.py` — 扩展预加载列和窗口
- `margin_detail`: 单日→60天窗口，加 `date` 列
- `analyst_forecast`: 加 `overweight_count, neutral_count, underweight_count`
- `fund_hold`: 加 `change_ratio`
- `lhb_detail`: 合并两个重复查询为1个90天窗口，加 `trade_date, circ_mv, post_5d`
- `fund_flow`: 单日→60天窗口，加 `date, main_net_ratio`

### 2. `_event.py` — 7个价格因子改为纯 aux 模式
- `compute_lhb_net_buy`: `aux["lhb"]` + pandas groupby 替代 SQL
- `compute_lhb_post_quality`: 加 `aux=None`，用 `aux["lhb"]`  替代 SQL
- `compute_margin_balance_chg`: 加 `aux=None`，用 `aux["margin"]` 替代 SQL
- `compute_margin_buy_ratio_price`: 加 `aux=None`，用 `aux["margin"]` 替代 SQL
- `compute_main_flow_ratio`: 用 `aux["fund_flow"]` 替代 SQL
- `compute_fund_change`: 加 `aux=None`，用 `aux["fund_hold"]` 替代 SQL
- `compute_analyst_buy`: 用 `aux["analyst"]` 替代 SQL
- aux 缺失时 `raise ValueError` 而非 DB 直连

### 3. `fundamental.py` — 删除 DB fallback
- `compute_margin_buy_ratio`: 删除 DB 直连 fallback → `raise ValueError`
- `compute_analyst_consensus`: 删除 DB 直连 fallback → `raise ValueError`

### 4. `_dispatch.py` (上一轮 test-v119)
- 用 `inspect.signature(fn)` 替代 `except TypeError` 判断是否传 `aux`
- 加 `_syms` 为空的防御守卫
- `compute_dt_streak` 还原原始签名（不含 aux，纯 OHLCV）

### 不变
- 3个纯 OHLCV 因子 (`limit_up_proximity`, `limit_up_streak`, `dt_streak`) 不碰 DB，也不接受 aux
- fundamental.py 中 `_get_financial_historical` 等数据层辅助函数保留其 DB 连接

### 未覆盖
- `compute_limit_up_proximity` (line 18-76) 和 `compute_limit_up_streak` (line 78-157) 仍有 `import sqlite3` 和 `_db_connect()`，待后续处理
