 # Optimizer Layer (Layer 5)

 Portfolio construction with capital-adaptive allocation and integer-lot constraints.

 ## Architecture

 ```
 optimizer/
 ├── portfolio.py    # PortfolioConstructor — capital-adaptive allocation
 ├── rebalance.py    # compute_trades() — target vs current → order list
 ├── kelly.py        # Kelly criterion position sizing
 ├── hyperopt.py     # Hyperparameter optimization (Optuna)
 └── README.md
 ```

 ## Capital-adaptive strategy

| Capital | Method | Rationale |
|---------|--------|-----------|
| < ¥20,000 | Equal-weight + integer-lot greedy | Lot constraint is rigid; equal-weight is the only stable solution |
| ¥20,000–100,000 | Score-weighted + integer rounding | 10–20 lots per stock; score-weighted with integer correction |
| > ¥100,000 | Mean-variance + integer-lot constraint | Single-stock weight < 0.5%; continuous approximation works |

 ## Key interfaces

 ```python
 from optimizer.portfolio import PortfolioConstructor
 from optimizer.rebalance import compute_trades

 pc = PortfolioConstructor(config)
 target = pc.construct(alpha, limits, capital)
 orders = compute_trades(target, current, cost_model)
 ```

 ## Related docs

 - [ARCHITECTURE.md — Layer 5](../ARCHITECTURE.md)
