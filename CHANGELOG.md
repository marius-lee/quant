## [P71] — 2026-07-09

### 代码安全与稳定性修复

**execution/engine.py — execute() 事务回滚与语法修复 ([31mCritical[0m)**

execute() 方法存在三个严重问题：
1. IndentationError — for 循环体未正确缩进 (第 116 行)，文件不可 import
2. 无 commit/rollback — 批量订单 BEGIN 后从不 COMMIT 也不 ROLLBACK
3. 无 close — 连接泄漏

修复：补全 for 循环体缩进，try/except/finally 模式保障 COMMIT/ROLLBACK/CLOSE。

**factor/compute.py — compute_str() SQL 注入 ([33mHigh[0m)**

第 2430 行：`WHERE symbol IN ('{"','".join(raw.index.tolist())}')` 直接拼接 symbol 值。
修复：替换为参数化查询 `WHERE symbol IN ({_ph2})` + bind params。

**factor/compute.py — compute_ocfp() 绕过连接层 ([33mHigh[0m)**

第 2575 行：`import sqlite3 as _sql; _sql.connect(_db_path)` 直接绕过 `market_conn("ro")`。
修复：替换为 `from data.store import market_conn; _conn = market_conn("ro")`。

**data/store.py — DataStore 线程安全 ([32mMedium[0m)**

DataStore._connect() 增加 `threading.Lock` 保护 shared _conn 创建，避免多线程竞态。
新增 _make_conn() 工厂方法，_connect 返回 thread-local 连接。

**config/**init**.py — 包 docstring ([32mLow[0m)**

之前为空文件，添加包级 docstring。

**全量 pyc 清理** — 删除所有 stale .pyc，解决 stats_cache 旧版本 devnull 残留。

---

## [P69] — 2026-07-09

### 架构清理: 因子注册表集中化 + 消除重复定义 + 连接层统一 + pipeline 抽取

**#1 因子注册表集中化**: factor/compute.py 中 33 个分散的动态注册块
(`if "xxx" not in _PRICE_FN_MAP` / `if "xxx" not in _FUNDAMENTAL_FN_MAP`)
全部迁移到静态 map 定义中，两 map 移至文件末尾确保前向引用安全。
_PRECE_FN_MAP: 27→38 entries, _FUNDAMENTAL_FN_MAP: 5→27 entries.

**#2 消除重复定义**: compute_margin_buy_ratio / compute_gross_margin_diff /
compute_financial_anomaly / compute_roe_trimmed 各定义两次（第一个版本被
第二个完全覆盖，约 200 行死代码）。保留完整版本，删除简化版本。
margin_buy_ratio 价格版重命名为 compute_margin_buy_ratio_price 避免冲突。

**#3 连接层统一**: factor/compute.py (11处) + web/app.py (3处) +
execution/engine.py (1处) 的 `sqlite3.connect(db)` 替换为 `market_conn('ro')`。
`update_factor_evaluation` 使用 `market_conn('rw')`。
TRADE_DB 连接保留原样（不同数据库）。

**#4 pipeline.py 抽取**: _post_state / _post_state_sync / _sanitize_for_json /
_state_url 从 pipeline.py 提取到 web/state_pusher.py。

### 文件变更
- `factor/compute.py`: -209/+58 行 (maps 集中化 + 去重)
- `factor/registry.py`: +1 行 (dividend_yield 加入 _FIN_FACTORS)
- `factor/synth.py`: → re-export from alpha.synth
- `alpha/__init__.py`: 空壳 → 实际导出 AlphaModel + synth functions
- `alpha/model.py`: +~80 行 (新建，pipeline.py Step 3 抽取)
- `alpha/synth.py`: 新建
- `data/store.py`: +market_conn()
- `web/app.py`: -3/+4 行 (market_conn 替换)
- `execution/engine.py`: -1/+2 行 (market_conn 替换)
- `web/state_pusher.py`: +67 行 (新建，HTTP 推送)
- `pipeline.py`: -60/+4 行 (抽取后简化为 5 行 Alpha 编排)
- `CHANGELOG.md`: 本文档
- `HANDOFF.md`: updated

---

## [3.6.0] — 2026-07-09

### P68: ProcessPoolExecutor 孤儿进程内存泄漏 — 根因修复

**问题**: web app 每次启动 / restart.sh 重启时, stats_cache.py=compute_factor_stats() 的 ProcessPoolExecutor(max_workers=6) spawn 6 个子进程。父进程被杀后子进程变孤儿 (PPID=1), 累积到 152 进程 / 9 GB RSS。SIGTERM 信号处理仅打日志, 不清理子进程。

**修复 (3 层防护)**:

1. `factor/stats_cache.py` (line 277): `executor.shutdown(wait=False)` -> `wait=True`, 确保正常返回时 worker 被 join
2. `factor/stats_cache.py` (line 53-75): 新增 `_ORPHAN_PID_FILE` + `_cleanup_process_pool()` — 写入 worker PID 到 `data/.compute_pids` 文件, 模块加载时自动清理上次残留, web app SIGTERM 时通过 `_clean_exit()` 读取文件杀所有 PID
3. `web/app.py` (line 21-43): 新增 `_clean_exit(reason)` 替代原 lambda — SIGTERM/SIGINT 时先读 `.compute_pids` 杀子进程, 再 `_sys.exit(0)`

### 双调度器清理

- **删除**: `scheduler.py` (根目录 standalone, 与 `quant/scheduler.py` daemon 线程重复调度)
- **删除**: `com.quant.scheduler.plist` / `com.quant.webapp.plist` (launchctl 定时服务)
- **删除**: `restart.sh` (每次调用产出一批孤儿进程的入口)

### 文件变更

- `factor/stats_cache.py`: +28 lines (PID 追踪 + cleanup)
- `web/app.py`: +16/-3 lines (信号处理改进)
- `CHANGELOG.md`: +22 lines (本文档)
- `HANDOFF.md`: updated

---

## [P67] — 2026-07-07

### 数源切换: Tushare → akshare (holder_trade + pledge_stat)
Tushare stk_holdertrade/pledge_stat 需 2000 积分, 无权限。切换为 akshare 免费源。

- data/holder_trade.py: ak.stock_shareholder_change_ths 逐只拉取
- data/pledge.py: ak.stock_gpzy_pledge_ratio_em 批拉全市场
- factor/compute.py: SQL 改用 change_vol, 移除 clip(-0.5, 0.5)
- dividend.py 保留 Tushare (120 积分门槛低)

---

## [P66] — 2026-07-07

### 新增 3 个因子 (Step 3/3): 大股东减持 + 股权质押 + 股息率
Step 3/3: 需新建 Tushare 数据模块的因子落地。至此 10 因子候选池完成。

- holder_reduction: 大股东减持 — 60 日内减持比例/总股本 (上交所 2020; 海通金工 2023)
- pledge_ratio: 股权质押比例 (中信建投 2022)
- dividend_yield: 股息率 — 12 月现金分红/股价 (中信金工 2023)
- 新建 data/holder_trade.py, data/pledge.py, data/dividend.py 三个 Tushare 数据模块
- factor_registry 注册, 总计 10 active factors

活跃因子数: 7 → 10

---

## [P65] — 2026-07-07

### SUE 因子 (标准化未预期盈余)
Step 2/3: SUE (PEAD) 因子集成。新增 total_shares 列支持。

- stocks 表新增 total_shares 列
- fundamental.py 存储 stock_value_em 返回的总股本
- compute_sue: 季度 EPS 同比 / 8季标准差
- 来源: Bernard & Thomas (1989); 中信金工 2022

活跃因子数: 6 → 7

---
## [P64] — 2026-07-07

### 新因子集成 (4/10)
Step 1/3: 数据就绪因子立即落地。从 A 股文献中筛选已验证有效的因子，替代原有 36 个低效因子。

- asset_growth: 资产增长率 (Cooper, Gulen & Schill 2008; 华泰 2023)
- gp_ta: 毛利/总资产 (Novy-Marx 2013; Fama-French 2015 RMW)
- ztd: 停牌比率 (Liu 2006, 中国市场流动性)
- northbound_20d: 北向资金净流入 (华泰 2023, 中金 2022)

活跃因子数: 2 → 6

---

## [P63] — 2026-07-07

### 优化器参数去硬编码
- 删除 equal_weight_cap / weighted_cap 硬编码阈值, 改为 _tier() 自动判定 (均价 x lot_size x 资金量) ⛔ 已弃用 → v33 改为 `nano_cap: 30000` / `micro_cap: 100000`
- risk_aversion 不写入 config.yaml, 由 calibrate_risk_aversion() 实时网格搜索 (lambda in {0.5, 1, 2, 5, 10})
- calibrate_risk_aversion() 为模块级独立纯函数, 不依赖 PortfolioConstructor 实例
- pipeline.py 协方差矩阵 cov 作用域提升, 传入 construct(covariance=cov)
- config.yaml 删除 optimizer.equal_weight_cap

---

# Changelog

All notable changes to this project will be documented in this file.

## [3.0.0] — 2026-07-02

### Architecture Redesign

Complete system architecture overhaul from Chen Xiaoqun board-trading system to a professional 7-layer quantitative factor-based stock selection system based on Grinold & Kahn Fundamental Law framework.

### Added

- **ARCHITECTURE.md** — Full system architecture design (7 layers, module interfaces, data flow, migration plan)
- **factor/** — Layer 2: Factor computation, IC/IR evaluation, decay analysis, synthesis
- **alpha/** — Layer 3: Alpha model for factor combination and cross-sectional ranking
- **risk/** — Layer 4: Industry/size neutralization, Ledoit-Wolf covariance, exposure constraints
- **optimizer/** — Layer 5: Capital-adaptive portfolio construction (equal-weight / score-weighted / mean-variance)
- **monitor/** — Layer 7: Performance attribution and risk reporting
- **web/static/favicon.svg** — App icon (dark theme bar chart)
- **web/static/plotly.min.js** — Local plotly.js (3.5MB, replaces CDN for fast loading)

### Changed

- **README.md** — Rewritten to reflect actual 7-layer architecture instead of aspirational factor ML system
- **CLAUDE.md** — Updated module guidance, removed Chen Xiaoqun phase gates
- **config/config.yaml** — Removed Chen-specific sections (strategy, demon, ranker, affordable); added factor, alpha, risk, optimizer sections
- **requirements.txt** — Updated dependencies (removed backtrader, added tickflow, flask, pytest)
- **web/app.py** — Stripped to core Flask API routes; removed intraday runner thread, strategy scheduler, Chen-specific routes
- **web/shared.py** — Strategy identifier changed from "chen" to "quant"
- **execution/quote.py** — Stripped BoardTracker class (~400 lines); retained fetch_quotes() only
- **web/static/style.css** — Complete redesign: dark theme, full-width bands, professional typography
- **web/static/app.js** — Complete rewrite: Plotly charts, deferred rendering, tab navigation, API polling
- **web/templates/index.html** — Complete rewrite: SPA with 4 tabs (Overview/Factors/Portfolio/Performance), KPI strip

### Removed

- **intraday_runner.py** (46KB) — Replaced by scheduler.py + pipeline.py
- **execution/sell_chain.py** (5.6KB) — Chen sell chain, replaced by risk/constraints.py
- **archive/** — 5 dead code files (db.py, live_broker.py, monitor.py, repository.py, risk_checker.py)
- **strategies/** — 4 strategy files (base.py, etf_rotation.py, market_timing.py, smallcap_rotation.py)
- **ops/** — 6 hardcoded stub files (liquidity.py, performance.py, position_sizers.py, review.py, sector_scan.py, signal_algo.py)
- **backtest/__init__.py** — Commission logic migrated to execution/cost.py
- **web/templates/** — 4 old templates (etf.html, smallcap.html, timing.html, arena.html)
- **web/static/** — 2 PWA files (manifest.json, sw.js), 2 icon SVGs (icon-192.svg, icon-512.svg)

### Design Decisions

- Factor evaluation uses cross-sectional Rank IC (Spearman) for robustness
- Covariance estimation uses Ledoit-Wolf shrinkage for high-dimensional stability
- Portfolio construction is capital-adaptive: <20k equal-weight, 20k-100k score-weighted, >100k mean-variance
- Unified CostModel ensures comparable performance metrics across all simulated trades
- Config-driven parameters with hot-reload support

## [3.1.0] — 2026-07-02

### Factor Layer Implementation (L2)

- **factor/base.py** — Factor ABC with `compute(data, date) → FactorResult` and `evaluate()` protocol. FactorResult and FactorStats dataclasses.
- **factor/compute.py** — 11 registered factors across 6 categories (momentum 5/10/20/60d, reversal 5d, volatility 20d, downside_vol 20d, vol_ratio 5d, turnover_chg 5d, amihud 20d, skewness 20d). All pure vectorized functions with z-score normalization.
- **factor/evaluate.py** — Cross-sectional Rank IC (Spearman), IC_IR, IC decay analysis across [1,5,20] horizons, factor correlation matrix (Spearman).
- **factor/synth.py** — Factor synthesis: equal_weight (simple average) and ic_weighted (weights ∝ |IC|) with z-score clipping.

### Alpha Layer Implementation (L3)

- **alpha/model.py** — AlphaModel with calibrate/predict/get_top_n/select_candidates API. Supports equal_weight and ic_weighted synthesis. Cross-sectional percentile ranking.

### Risk Layer Implementation (L4)

- **risk/neutralize.py** — Industry neutralization (within-industry z-score) and size neutralization (OLS residual vs log market cap).
- **risk/covariance.py** — Ledoit-Wolf (2004) shrinkage covariance with optimal δ auto-estimation via constant-correlation target matrix.
- **risk/constraints.py** — RiskLimits dataclass, liquidity filter (min ¥5M daily amount), price filter (min ¥2), ST filter, position limit checks, sector exposure checks.

### Optimizer Layer Implementation (L5)

- **optimizer/portfolio.py** — PortfolioConstructor with 3-tier capital-adaptive strategy: <¥20k equal-weight greedy, ¥20k-100k score-weighted rounding, >¥100k mean-variance + integer lot constraint.
- **optimizer/rebalance.py** — compute_trades (target vs current → buy/sell orders), turnover limit enforcement, order validation.

### Execution Layer Implementation (L6)

- **execution/cost.py** — Unified CostModel (commission 0.03%, min ¥5, stamp 0.1% sell-only, slippage 0.1% both sides).
- **execution/engine.py** — ExecutionEngine with trades.db persistence, capital tracking, position querying.

### Monitor Layer Implementation (L7)

- **monitor/attribution.py** — Brinson (1986) performance attribution, factor exposure OLS decomposition, Sharpe ratio (annualized), max drawdown, win rate.
- **monitor/report.py** — Daily report generation → JSON → web/shared.py push.

### Orchestration

- **pipeline.py** — 7-layer pipeline (data → factor → alpha → risk → optimizer → execution → monitor) with independent try/except per layer.
- **scheduler.py** — Trading-day 15:30 auto-trigger loop.

### Verification

- Factor IC/IR tested on 200 stocks × 55 periods: momentum_10d IC=0.024, IC_IR=0.253 (strongest signal)
- Full pipeline end-to-end: 1000 stocks → 495 candidates → 18s runtime
- All 11 factors compute with correct z-score normalization (mean≈0, std≈1)

## [3.2.0] — 2026-07-03

### Factor Model & Backtest

- **P3: zt_streak 激活** — 涨停连板因子从 daily OHLCV 自算，回测 +80.5% (Sharpe 2.10, α+75.0%)
- **P4: factor_cache 迁数据库** — factor_snapshot + factor_registry 两表，前端/管道均读 DB
- **Phase 10: 3新因子审计** (high52w_dist, dt_streak, vol_price_corr_10d) — IC 优秀但回测低于基线，回归 5 因子

### Config & Parameter Management

- **P11: 因子评估标准重构** — 固定 IC 阈值 → 统计推断 (Grinold & Kahn 1999)
- **P13: 规范修订** — 模板2作用域限定 + 模板3 TDD降级为软约束
- **P37: 因子窗口参数化** — config.yaml 驱动 amihud/skewness/ma_alignment
- **P38: 全量参数配置化** — 15个遗漏参数迁入 config.yaml
- **P39: config.yaml 默认值切换** — 开发测试 → 生产标准

### Coding Standards Audit

- **P12: IO-计算分离** — 因子函数移除 DataStore 依赖 (模板 2a)
- **P14-P16, P18-P22**: 编码规范审计 — 安全/性能/API/数据模型/防御性编程/并发/可观测性全面审查修复

## [3.3.0] — 2026-07-04

### Factor & Evaluation Pipeline

- **P33: n_symbols/lookback 标准化** — 中证800 + 120交易日
- **P42: eval_stepwise.sh** — eval 参数从 config.yaml 读取 (_ecfg)
- **P43: 多因子分仓架构 (sleeve)** — sleeve_compose + combine_mode 分支, positions_per_factor=20
- **P44: 回测窗口扩展** — 6个月 → 3.5年 (850d+)
- **P45: 因子评估死锁修复** — 评估管道绕过 status='active' 过滤，35因子全量评估

### Data Sources

- **P25-P28**: 数据源全面审查 + 6源连通性测试 + akshare/同花顺/雪球接入评估
- **P26: 凭证管理** — config/env.example 模板 + .env 自动加载
- **P29-P30**: tushare 接入 + daily_basic fallback (PE/PB 真空填补)

## [3.4.0] — 2026-07-05

### Pipeline Architecture

- **P49: 两阶段 Pipeline + 三时段调度** — generate_signals(08:30) / execute_signals(09:30) / daily_sync(15:30)
- **P50: 调度日志统一** — [SCHEDULER] 标记三阶段完成日志
- **P51: restart.sh** — 一键启动 web + launchd scheduler

### Pipeline Bugfixes

- **P48: 消除 11 处静默 except-pass** — 核心路径加 logger.debug/warning
- **P52: daily_sync 修复** — cfg import scope + sync_all(conn) 参数
- **P53: pre-commit-check.sh** — 含 pycache 清理

### Factor Model Evolution

- **P46: zt_streak 最终验证** — 6个月回测确认 alpha 贡献
- **P54: 资金追踪统一** — strategy_config.cash_balance 作为单一真相源

## [3.5.0] — 2026-07-06

### Bugfix & Anti-Pattern Cleanup

- **ADR 023 Part A: 7个因子管道 Bug** — amihud min_valid 自适应, ma_alignment window 参数, 5个重复因子删除, 2个死因子删除
- **ADR 023 Part B: 全代码反模式清除** — 8处 fail-fast (config 缺失→崩溃), 4处静默 except 修复
- **ADR 023 Part C: 废弃引用全面清除** — 活跃代码禁止引用已删除因子
- **ADR 023 Part D: 关键路径埋点补齐** — _cs_zscore NaN/amihud全过滤/compute_all汇总/pipeline step4/backtest turnover

### Web & UI (P55-P57)

- **P55: 日志轮转** — 每天新建日志文件，保留10天
- **P56: 进程保活 + 状态跨进程共享 + 资金显示修复**
- **P57: 界面全面审计** — 24项修复含 KPI 核验/交易次数（买/卖）/胜率/PnL%/日志清理
- **P57: 实时报价** — 新浪实时行情 overlay，盘中概览 KPI 随市价变动
- **P57: 估值三级回退** — 盘中(新浪实时) → 盘后(daily.close) → 极端(成本价)
- **P57: 持仓表实时刷新** — 5s 轮询 /api/quotes
- **P57: 风险暴露图** — /api/risk, 60日滚动年化波动率+最大回撤，Plotly 柱状图
- **P57: 交易时间格式** — YYYY-MM-DD HH:MM:SS + 持仓买入时间列

### Architecture Rules

- config.yaml 为单一真相源 — 行为参数缺失必须 fail-fast
- 先读目标代码，确认已有模式，最小改动贴合 — 纳入 CLAUDE.md 工作规则
- Data quirks: cash balance 缺口 = 佣金+滑点 (CostModel 文档化)


## [3.5.3] — 2026-07-06

### P60: 硬编码数值参数全部挪至 config.yaml — 单一真相源

- **核心原则**: config/config.yaml 是项目中所有可配置参数的单一真相源。代码中不再保留任何硬编码默认值。
- **15 files changed**:
- config.yaml: risk 新增 min_price=2, max_sector_exposure=0.40; factor 新增 amihud/turnover_rev/idio_vol/high52w/roe_*/debt_ratio/accruals/synth/stats 校准参数; calendar 新增 max_lookup_days=30
- risk/constraints.py: RiskLimits.from_config() 从 yaml 读取所有约束参数; 修复 apply_all_filters() RiskLimits() 无参调用 crash; 修复 filter_st_stocks 非字符串 name crash
- data/trade_repo.py: SQL DDL 移除硬编码 DEFAULT 5000/0.08/20; 添加注释说明来源 config.yaml
- backtest.py: CLI fallback `else 5000` → `cfg("backtest.default_capital", 100000)`
- 8 files (pipeline.py, scheduler.py, web/app.py, web/state_broker.py, monitor/report.py, optimizer/, execution/calendar.py, factor/compute.py): 全部散落硬编码 → cfg() 读取
- **审计文档**: docs/audit_magic_numbers_20260706.md (67 files 逐行审查, 16 项)

## [3.5.4] — 2026-07-06

### P61: 审计收尾 — 消除最后6项硬编码 + neutralize 路径统一

- factor/synth.py: sleeeve_compose() 去掉 positions_per_factor=8, min_factors=1 默认值
- neutralize.py + pipeline.py + config: risk.neutralization.industry_min_count → risk.neutralize.min_common_stocks (统一命名，两处都是 Fama-French 1993 OLS 最小样本量 30)
- web/app.py, data/cache.py, data/store.py: 加注释说明非业务参数 (limit=10000, _local_burst, cache_size=-64000)
- attribution.py + config: rf=0.02, periods=252 挪到 config attribution.risk_free_rate + attribution.annual_periods
- 审计文档 16 项全部闭合

## [3.5.5] — 2026-07-07

### P62: factor_registry 修复 + validate 改进

- factor_registry: dt_streak status deprecated → active (漏入: dt_streak 在 eval 中通过 Layer 1 t-test + Layer 2 边际 IC)
- validate.py: deprecated 因子警告信息补充说明 IC 阈值 ≠ 统计显著 (需跑 Layer 1+2 全量 eval)
- validate.py: extreme returns 查询排除 BJ (30% limit) 和仙股 (<2), 改进警告信息
- 回测/实盘数据隔离验证: trades.db 中 backtest 和 quant 策略完全隔离

## Factor Count Evolution

| 版本 | 因子数 | Active | 备注 |
|------|--------|--------|------|
| v3.0 | 11 | — | 初始设计 |
| v3.3 | 35 | 5 | bp_ratio+size+gap_5d+zt_streak+amihud_20d |
| v3.4-v3.5 | 35 | 1 | zt_streak 唯一通过步进回测 |
| v3.5.5 (P60-P62) | 36 | 2 | 硬编码全部挪 config.yaml, 审计 16 项闭合, validate 改进 |
| v3.5.1 (P58) | 36 | 2 | +residual_momentum_126d (IC=-0.0027, A股不成立), dt_streak activated, backtest策略隔离修复 |


## [3.5.1] — 2026-07-06

### P58: 14次提交 — 文档审计 + residual_momentum + 回测隔离 + DB锁 + dt_streak + 界面 + eval防护 + schema统一

- **14文件文档审计**: 统一因子数 35→36, ADR 状态更新, CHANGELOG 补全 v3.2-v3.5, 旧 HANDOFF 加归档标记
- **residual_momentum_126d**: Kakushadze & Serur (2018) Ch.3.7 残差动量落地, 36th factor
- **backtest.py 策略隔离修复**: 6处硬编码 `"quant"` 改为 `STRATEGY="backtest"` 变量
- **sqlite3 busy_timeout 全线修复**: daily_sync/factor_compute/stats_cache/eval_stepwise 所有 market.db 写连接加 timeout=30, 消除回测污染实盘数据的风险 (commit e3f1aca 的修复仅改了1/5处)


## [3.5.2] — 2026-07-06

### P59: engine.get_capital pos_value bug — eval stepwise 步进回测 Wealth=¥0 修复

- **根因**: `ExecutionEngine.get_capital()` 中 `p.get('value', 0)` 永远返回 0, 因为 `TradeRepo.get_positions()` 返回的 dict 键是 `symbol,price,shares,board_count,buy_time`, 没有 `value`
- **影响**: `get_capital()` 只返回现金不包含持仓市值, 导致回测 total_wealth 偏小, 下轮资本预算萎缩, 财富逐轮衰减到零
- **修复**: `p.get('value', 0)` → `(p.get('price',0) or 0) * (p.get('shares',0) or 0)`
- **eval_stepwise.sh**: `stderr=subprocess.DEVNULL` → `subprocess.PIPE` + 正则失败 debug 打印 (防止静默错误)
- **验证**: 修复前 wealth=¥16,527 (pipeline total=¥99,847), 修复后 wealth=¥96,799 (完全一致)
