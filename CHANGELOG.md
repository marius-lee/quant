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
