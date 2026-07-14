 # Risk Layer (Layer 4)

 Risk adjustment for alpha scores: sector neutralization, covariance estimation, exposure constraints.

 ## Architecture

 ```
 risk/
 ├── neutralize.py   # Industry + size neutralization (cross-sectional regression residuals)
 ├── covariance.py   # Ledoit-Wolf (2004) shrinkage covariance estimation
 ├── constraints.py  # RiskLimits — single-stock/industry caps, liquidity filters
 ├── var.py          # Value-at-Risk estimation
 ├── atr.py          # Average True Range (volatility stop)
 └── README.md
 ```

 ## Key interfaces

 ```python
 from risk.neutralize import industry_neutralize, size_neutralize
 from risk.covariance import ledoit_wolf_cov
 from risk.constraints import RiskLimits

 scores = industry_neutralize(alpha, industries)
 cov = ledoit_wolf_cov(returns, shrinkage=0.3)
 limits = RiskLimits(max_single=0.05, max_positions=20)
 candidates = limits.filter(df)
 ```

 ## Risk doesn't add alpha — it only subtracts and constrains.

 ## Related docs

 - [ARCHITECTURE.md — Layer 4](../ARCHITECTURE.md)
