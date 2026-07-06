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

## Factor Count Evolution

| 版本 | 因子数 | Active | 备注 |
|------|--------|--------|------|
| v3.0 | 11 | — | 初始设计 |
| v3.3 | 35 | 5 | bp_ratio+size+gap_5d+zt_streak+amihud_20d |
| v3.4-v3.5 | 35 | 1 | zt_streak 唯一通过步进回测 |
| v3.5.1 (P58) | 36 | 1 | +residual_momentum_126d (Ch.3.7, 待eval) |
