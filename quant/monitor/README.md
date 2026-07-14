 # Monitor Layer (Layer 7)

 Post-market performance attribution and risk reporting.

 ## Architecture

 ```
 monitor/
 ├── attribution.py  # Brinson attribution + IC decay detection (active→monitoring→retired)
 ├── report.py       # generate_report() — daily JSON + Web push
├── metrics.py      # Performance metrics (Sharpe, Sortino, MDD, Calmar)
 ├── metrics.py      # Performance metrics (Sharpe, Sortino, MDD, Calmar)
 ├── alerts.py       # Alert conditions and notification triggers
├── notify.py       # Notification delivery (terminal, file)
 ├── notify.py       # Notification delivery (terminal, file)
 └── README.md
 ```

 ## Daily report structure

 ```json
 {
   "date": "2026-07-14",
   "pnl": {"realized": 123.45, "unrealized": -67.89},
   "positions": [{"symbol": "000001", "shares": 100, "price": 12.34, "pnl": 50.00}],
   "exposure": {"sectors": {"银行": 0.15, "食品饮料": 0.10}},
   "metrics": {"sharpe_rolling_20d": 0.85, "max_drawdown": 0.12}
 }
 ```

 ## Key interfaces

 ```python
 from monitor.report import generate_report
 from monitor.attribution import brinson_attribution

 report = generate_report(date, trade_repo)
 attr = brinson_attribution(returns, factor_exposures)
 ```

 ## Related docs

 - [ARCHITECTURE.md — Layer 7](../ARCHITECTURE.md)
