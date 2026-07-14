 # Architecture Overview

 The system follows Grinold & Kahn's Fundamental Law of Active Management:

 **IR (Information Ratio) = IC (Information Coefficient) × sqrt(Breadth)**

 This decomposes into seven layers:

 | Layer | Module | Role |
 |-------|--------|------|
 | 0 | config/, utils/ | Configuration, logging, calendar |
 | 1 | data/ | Daily data sync (5 sources), trade persistence, repos |
 | 2 | factor/ | Factor computation (57 factors), IC/IR evaluation |
 | 3 | alpha/ | Factor synthesis → expected return → cross-sectional ranking |
 | 4 | risk/ | Sector/size neutralization, Ledoit-Wolf covariance, exposure limits |
 | 5 | optimizer/ | Capital-adaptive portfolio construction, rebalance |
 | 6 | execution/ | Simulated trading, unified cost model, order recording |
 | 7 | monitor/ | Brinson attribution, daily reporting, Web push |

 ## Design principles

 1. **Layered decoupling**: Each layer depends only on lower-layer interfaces
 2. **Traceable parameters**: Every threshold has a documented source (math / literature / calibration)
 3. **Backtest-first**: All signal generation must run independently on historical data
 4. **Zero redundancy**: Every module has a clear call site; uncalled code is removed
 5. **Configuration-driven**: Thresholds, windows, weights all from config.yaml
 6. **North star**: All decisions orbit the ¥5,000 → ¥1M target

 ## Signal flow

 ```
 Data → Factor → Alpha → Risk → Optimizer → Execution → Monitor → Web
 ```

 Signals flow bottom-up. Orders flow top-down.

 ## Related docs

 - [ARCHITECTURE.md](../../ARCHITECTURE.md) — Full architecture detail (v3.0)
 - [Seven layers](../../ARCHITECTURE.md) — Layer-by-layer spec
 - [Data dictionary](../../docs/architecture/DATA_DICTIONARY.md) — Complete schema
 - [ADR catalog](../../docs/adr/) — Architecture Decision Records
