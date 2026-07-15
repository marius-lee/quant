 # Evaluation Pipeline

 Five-phase standard evaluation pipeline for factor certification (ADR 026).

 ## Architecture

 ```
 evaluation/
 ├── phase1_data.py      # Phase 1: Data preparation (stock universe, date ranges)
 ├── phase2_single.py    # Phase 2: Single-factor tests (RankIC, |t|, ICIR, half-life)
 ├── phase3_oos.py       # Phase 3: Walk-forward validation (CPCV + PBO)
 ├── phase4_costs.py     # Phase 4: Cost-adjusted backtest
 ├── phase5_monitor.py   # Phase 5: Ongoing evaluation monitoring (rejects factors that fail prior phases)
 ├── phase6_backtest.py  # Phase 6: Full backtest (evaluation pipeline backtest, not live trading)
 ├── phase7_wf.py        # Phase 7: Walk-forward optimization (pending, ADR 029 P1/P2)
 ├── cpcv.py             # Combinatorial Purged Cross-Validation
 ├── pbo.py              # Probability of Backtest Overfitting
 ├── parallel.py         # Parallel evaluation with ProcessPoolExecutor
 ├── run_store.py        # Evaluation result persistence (evaluation_runs table)
 └── README.md
 ```

 ## Pipeline flow

 ```
 Phase 1 (Data) → Phase 2 (Single-factor IC) → Phase 3 (Walk-forward + PBO)
    → Phase 4 (Cost-adjusted backtest) → Phase 5 (Monitoring)
      → Phase 6 (Full backtest, PENDING) → Phase 7 (Walk-forward optimization, PENDING)

Note: phases are independent scripts. `run_store.py` provides shared persistence.
No centralized pipeline orchestrator exists — run phases manually in sequence.
 ```

 ## Running the pipeline

 ```bash
 # Full standard evaluation
 bash scripts/eval_standard.sh

 # Individual phases
 PYTHONPATH=. python3 -m evaluation.phase2_single
 PYTHONPATH=. python3 -m evaluation.phase3_oos
 ```

 ## Thresholds (config.yaml → factor.evaluation)

| Metric | Threshold | Source |
|--------|-----------|--------|
| |RankIC| | ≥ 0.02 | Broker consensus |
| |t| | ≥ 2.0 | 95% confidence |
| ICIR | ≥ 0.5 | Minghong/Linjun standard |
| Half-life | ≥ 20 days | Monthly rebalance |
| PBO | < 0.3 | Bailey et al. (2015) |
| Sharpe decay | < 50% | Walk-forward validation |

 ## Related docs

 - [ADR 026: Standard evaluation workflow](../docs/adr/026-standard-evaluation-workflow.md)
 - [Evaluation pipeline details](../docs/factors/evaluation-pipeline.md)
