 # Core Module

 Shared abstractions and utilities that span the entire system.

 ## Architecture

 ```
 core/
 ├── trace.py          # Experiment + Trace system (DAG-based experiment tracking)
 ├── phase_tracker.py  # Phase tracking for factor evaluation
 ├── version.py        # Single-source version (read by pyproject.toml)
 └── README.md
 ```

 ## Trace system

 The `core/trace.py` module provides a lightweight DAG-based experiment trace:

 ```python
 from core.trace import get_trace, make_experiment, Hypothesis, ExperimentFeedback

 trace = get_trace()

 exp = make_experiment(
     action="factor_eval",
     hypothesis=Hypothesis("momentum_63d has IC > 0.02")),
 )
 exp.feedback = ExperimentFeedback(decision=True, reason="IC = 0.034, p < 0.01")
 trace.record(exp)

 sota = trace.get_sota("sharpe")
 ```

 Every evaluation run, backtest variant, and factor lifecycle event is recorded as an
 Experiment node in the Trace DAG, persisted to `experiments` table in market.db.

 ## Design principles

- No dependency on any Layer 1-7 module (only config)
- All abstractions are lightweight and computational (no LLM integration)
- Persistence through market.db tables, not external databases
