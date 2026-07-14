 # A-share Quantitative Stock Selection System

 Grinold & Kahn 7-layer architecture. Factor-driven, risk-neutral, portfolio-optimized, simulation-executed full pipeline. ¥5,000 → ¥1,000,000 (200x, 6 months).

 [![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org)
 [![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

 ---

 ## Architecture

 ```
 Layer 0: Config     — YAML hot-reload, constants, logging, calendar
 Layer 1: Data       — Multi-source daily sync, trade persistence, repos
 Layer 2: Factor     — 57 factors (41 price + 16 fundamental), IC/IR evaluation
 Layer 3: Alpha      — Factor synthesis → return prediction → cross-sectional ranking
 Layer 4: Risk       — Sector neutralization, Ledoit-Wolf covariance, constraints
 Layer 5: Optimizer  — Capital-adaptive portfolio construction with integer-lot constraints
 Layer 6: Execution  — Simulated trading engine, unified cost model, real-time quotes
 Layer 7: Monitor    — Brinson attribution, daily reports, Web push
          Web        — Flask dashboard on port 8521
 ```

 ## Quick start

 ```bash
 cd /Users/mariusto/project/quant

 # Install
 pip install -e ".[dev]"

 # Web service (includes scheduler thread)
 PYTHONPATH=. python3 web/app.py
 # → http://localhost:8521

 # Manual full pipeline
 PYTHONPATH=. python3 pipeline.py

 # Factor evaluation
 bash scripts/eval_standard.sh

 # Run tests
 pytest -v
 ```

 ## Directory

 ```
 quant/
 ├── alpha/          Layer 3: Alpha model
 ├── backtest/       Four-layer backtest engine
 ├── benchmark/      Benchmark tracking
 ├── config/         Layer 0: Config + constants
 ├── core/           Shared abstractions (Trace, Experiment)
 ├── data/           Layer 1: Data store + repos
 │   └── repos/      Repository layer
 ├── evaluation/     Five-phase evaluation pipeline
 ├── execution/      Layer 6: Execution engine
 ├── factor/         Layer 2: Factor computation
 │   ├── cards/      Factor index cards (JSON)
 │   └── compute/    Compute functions (price + fundamental)
 ├── monitor/        Layer 7: Attribution + reports
 ├── optimizer/      Layer 5: Portfolio construction
 ├── quant/scheduler/ Scheduler (orchestrator + weekly)
 ├── regime/         Market regime detection
 ├── risk/           Layer 4: Risk management
 ├── scripts/        Operational scripts
 ├── tests/          Test suite (67 tests)
 ├── utils/          Utilities (date, logger)
 ├── web/            Flask dashboard
 ├── docs/           Documentation
 │   ├── adr/        Architecture Decision Records (31)
 │   ├── architecture/ Data dictionary, data sources
 │   ├── backtest/   Backtest system docs
 │   ├── factors/    Factor catalog + evaluation
 │   ├── reports/    Audit and analysis reports
 │   └── research/   Factor research papers
 ├── ARCHITECTURE.md Detailed design (v3.0)
 ├── CLAUDE.md       Developer guide for AI assistants
 ├── CONTRIBUTING.md Contribution guide
 ├── CHANGELOG.md    Version history
 ├── pyproject.toml  Package config + lint/test tools
 ├── pipeline.py     Full pipeline orchestrator
 ├── requirements.txt        Runtime deps
 └── requirements-dev.txt    Dev deps
 ```

 ## Data flow

 ```
 Trading day → quant/scheduler/ → pipeline.py
   Step 1: DataStore.update_daily()
   Step 2: Factor.compute() → rank_ic()
   Step 3: AlphaModel.predict()
   Step 4: RiskManager.apply()
   Step 5: PortfolioConstructor.construct()
   Step 6: ExecutionEngine.execute()
   Step 7: Monitor.generate_report()
 ```

 ## Key decisions

 | Decision | Choice | Why |
 |----------|--------|-----|
 | Storage | SQLite | Single-user, zero-config, 10M+ rows |
 | Frequency | Daily | A-share T+1 |
 | Factor eval | Rank IC | Robust to outliers |
 | Covariance | Ledoit-Wolf | Better than sample for high dim |
 | Portfolio | Capital-adaptive | Upgrades with capital scale |
 | Cost model | Unified CostModel | Comparable across runs |
 | Parameter mgmt | YAML + hot-reload | Zero-downtime tuning |

 ## Documentation

 | Document | Content |
 |----------|---------|
 | [ARCHITECTURE.md](ARCHITECTURE.md) | Full architecture design (v3.0) |
 | [CLAUDE.md](CLAUDE.md) | Developer guide for AI coding assistants |
 | [CHANGELOG.md](CHANGELOG.md) | Version history |
 | [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guide |
 | [docs/architecture/](docs/architecture/) | Data dictionary, source catalog |
 | [docs/adr/](docs/adr/) | Architecture Decision Records |
 | [docs/research/](docs/research/) | Factor research papers |
 | [docs/reports/](docs/reports/) | Audit and analysis reports |
 | [factor/cards/](factor/cards/) | Factor index cards (JSON) |

 ## Inspiration

 Architecture patterns adapted from [Microsoft RD-Agent](https://github.com/microsoft/RD-Agent): Experiment + Trace system, factor index cards, documentation layout.

 ## Disclaimer

 This system is for educational and research purposes only. Stock market investment carries risk. System output does not constitute investment advice.
