 # Backtest Engine

 Four-layer backtest architecture (ADR 029): factor evaluation → signal synthesis → portfolio construction → performance attribution.

 ## Architecture

 ```
 backtest/
 ├── bt_engine.py      # Core backtest engine (event-driven)
 ├── broker.py         # Simulated broker (order execution, cost deduction)
 ├── loop.py           # Backtest loop entry point
 ├── diagnostics.py    # Pre-backtest rolling IC + post-backtest diagnosis
 ├── naming.py         # Naming conventions for backtest runs
 └── README.md
 ```

 ## Four-layer progression (ADR 029)

| Priority | Layer | Status | Description |
|----------|-------|--------|-------------|
| P0 | Diagnostics | Landed (backtest/diagnostics.py) | Rolling IC + factor tracking + auto-diagnosis |
| P1 | Walk-forward | Pending (ADR 029) | Rolling train/test windows, closed-loop with evaluation/ |
| P2 | Optimization | Pending (ADR 029) | Optuna parameter search |
| P3 | Attribution | Landed (attribution.py, 15:30) | Brinson + IC degradation (active→monitoring→retired) |

 ## Quick start

 ```python
 from backtest.loop import run_backtest
 result = run_backtest(
     start="2024-01-01",
     end="2025-12-31",
     strategy="momentum_value",
     capital=100000,
 )
 print(f"Sharpe: {result['sharpe']:.2f}, MDD: {result['max_drawdown']:.2%}")
 ```

 ## Related docs

 - [ADR 029: Four-layer backtest architecture](../docs/adr/029-four-layer-backtest.md)
 - [Backtest architecture](../docs/backtest/architecture.md)
