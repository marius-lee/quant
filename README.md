 # A-share Quantitative Stock Selection System

 Grinold & Kahn 7-layer architecture. Factor-driven, risk-neutral, portfolio-optimized, simulation-executed full pipeline. ¥5,000 → ¥1,000,000 (200x, 6 months).

 [![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org)
 [![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

 ---

 ## Architecture

 ```
 Layer 0: Config     — YAML hot-reload, constants, logging, calendar
 Layer 1: Data       — Multi-source daily sync, trade persistence, repos
 Layer 2: Factor     — 65 factors (57 price + 8 fundamental), winsorize+MAD z-score, IC/IR evaluation
 Layer 3: Alpha      — Factor synthesis (sleeve/IC-weighted/intersection/LGB) → return prediction → ranking
 Layer 4: Risk       — Sector neutralization, Ledoit-Wolf covariance, constraints
 Layer 5: Optimizer  — Capital-adaptive 3-tier (Nano/Micro/Small) + Grinold α−λ·TC cost band
 Layer 6: Execution  — Template Method execution (backtest/live shared), unified cost model, broker adapter
 Layer 7: Monitor    — Brinson attribution, ATR stop-loss, daily reports, Web push
          Web        — Flask dashboard on port 8521
 ```

 ## Quick start

 ```bash
 cd /Users/mariusto/project/quant

 # Install
 pip install -e ".[dev]"

 # Web service + scheduler (recommended; port 8521)
 bash scripts/restart.sh
 # → http://localhost:8521

 # Manual single-task run (orchestrator bypass)
 bash scripts/run_task.sh <task> [date]

 # Factor evaluation
 bash scripts/eval_standard.sh

 # Run tests (must use .venv — optuna/hmmlearn live there)
 PYTHONPATH=. .venv/bin/python3 -m pytest test/ -v
 ```

 ## Directory

 ```
 quant/
 ├── alpha/          Layer 3: Alpha model (sleeve/IC/LGB + rotation + multi-tf)
 ├── backtest/       Four-layer backtest engine
 ├── benchmark/      Benchmark tracking
 ├── config/         Layer 0: Config + constants
 ├── core/           Shared abstractions (Trace, Experiment)
 ├── data/           Layer 1: Data store + repos
 │   └── repos/      Repository layer
 ├── evaluation/     Eight-phase evaluation pipeline
 ├── execution/      Layer 6: Execution engine + broker adapter
 ├── factor/         Layer 2: Factor computation
 │   ├── cards/      Factor index cards (JSON)
 │   └── compute/    Compute functions (price + fundamental)
 ├── monitor/        Layer 7: Attribution + reports + ATR stop-loss
 ├── optimizer/      Layer 5: Portfolio construction
 ├── quant/scheduler/  Scheduler — manifest 任务声明 + 单一 orchestrator 主循环
 ├── regime/         Market regime detection
 ├── risk/           Layer 4: Risk management
 ├── scripts/        Operational scripts
 ├── test/           Test suite (336 tests)
 ├── utils/          Utilities (date, logger)
 ├── web/            Flask dashboard
 ├── docs/           Documentation
 │   ├── adr/        Architecture Decision Records (37+)
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
Trading day → quant/scheduler/ (manifest 声明式任务表 + 单一 orchestrator 主循环, v428)
   主循环每 30s 轮询: 窗口命中 + 依赖满足 → _dispatch 执行 (非交易日休眠)
   ├─ 盘前 signals (08:00-15:30): pipeline.generate_signals()
   │    Step 1: DataStore.update_daily() → Step 2: universe 预筛
   │    Step 3: FactorStore.load() → AlphaModel.combine()/rank()
   │    Step 4: neutralize + Ledoit-Wolf covariance + VaR
   │    Step 5: PortfolioConstructor.construct() → target_positions
   ├─ 盘中 execute (09:20-14:56, 依赖 signals 尝试过): pipeline.execute_signals()
   │    Step 6: ExecutionModel.run() → ExecutionEngine.execute() → trades.db
   │    Step 7: Monitor.generate_report() → push_to_web()
   ├─ 盘中 monitor (09:35-15:00): 实时风控守护线程 (ATR止损/止盈/熔断)
   ├─ 收盘 snapshot_close (15:00-15:05, 原 14:55 收盘前修正) → reconcile (15:05)
   ├─ 晚间 evening_chain (19:00-23:59, subprocess): daily_data → factor_cache → attribution
   └─ 周六 weekly_eval (06:00-12:00, subprocess): 周度因子评估
```

 ## Key decisions

 | Decision | Choice | Why |
 |----------|--------|-----|
 | Storage | SQLite | Single-user, zero-config, 10M+ rows |
 | Frequency | Daily | A-share T+1 |
 | Factor normalization | Winsorize+MAD z-score | Barra USE4 standard (ADR-037) |
 | Alpha synthesis | Sleeve (default) / IC-weighted / LGB | ML nonlinear upgrade (ADR-035) |
 | Covariance | Ledoit-Wolf | Better than sample for high dim |
 | Portfolio | Capital-adaptive 3-tier | Nano/Micro/Small auto-upgrade |
 | Execution | Template Method (backtest/live) | Shared chain; broker adapter (ADR-036) |
| Cost model | Unified CostModel | Commission+stamp(0.05%)+Almgren-Chriss impact |
| Parameter mgmt | YAML + hot-reload | Zero-downtime tuning |
| Scheduling | Manifest 声明式 + 单一 orchestrator 主循环 | 窗口/依赖/超时单一真相源 (v428, ADR 见 HANDOFF) |

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
