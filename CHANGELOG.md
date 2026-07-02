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
