 # Scheduler

 Trading-day orchestrator that drives the full pipeline on schedule.

 ## Architecture

 ```
 quant/scheduler/
 ├── orchestrator.py  # Main orchestrator — runs pipeline on trading days
 ├── signals.py       # Pre-market signal generation (08:30)
 ├── execute.py       # Market-open order execution (09:30)
 ├── attribution.py   # Post-market performance attribution (15:30)
 ├── monitor.py       # Signal monitoring and alerts
 ├── weekly.py        # Weekly rebalance logic
 ├── status.py        # Scheduler status tracking
 ├── _base.py         # Base scheduler class
 └── __init__.py
 ```

 ## Schedule

| Time | Action |
|------|--------|
| 08:30 | Generate signals |
| 09:30 | Execute orders |
| 15:30 | Run attribution + report |
| Weekly | Rebalance + factor evaluation |

 ## Usage

 ```bash
 # Start scheduler (via web/app.py)
 PYTHONPATH=. python3 web/app.py
 # Scheduler thread auto-starts within Flask app
 ```
