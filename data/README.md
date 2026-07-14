 # Data Layer (Layer 1)

 Multi-source daily data synchronization, trade record persistence, and structured repository access.

 ## Architecture

 ```
 data/
 ├── store.py           # DataStore — multi-source daily sync (tickflow→sina→tencent→tushare→akshare)
 ├── repos/             # Repository layer (single access point for all DB tables)
 │   ├── _base.py       #   BaseRepo — shared SQLite connection management
 │   ├── trade_repo.py  #   TradeRepo — sim_trades CRUD
 │   ├── factor_repo.py #   FactorRepo — factor_registry CRUD
 │   ├── evaluation_repo.py  # EvaluationRepo — evaluation_runs CRUD
 │   └── universe_repo.py    # UniverseRepo — stock universe
 ├── market.db          # SQLite data warehouse (~400MB)
 ├── benchmark.py       # Benchmark data (CSI 300)
 ├── analyst.py         # Analyst forecast data
 ├── daily_basic.py     # Daily basic data
 ├── dividend.py        # Dividend data
 ├── fund_flow.py       # Fund flow data
 ├── fund_hold.py       # Institutional holdings
 ├── fundamental.py     # Fundamental data
 ├── holder_trade.py    # Insider trading data
 ├── jq_financials.py   # JoinQuant financial data
 ├── jq_valuation.py    # JoinQuant valuation data
 ├── lhb.py             # Dragon-tiger board data
 ├── limit_up.py        # Limit-up pool data
 ├── macro.py           # Macro-economic data
 ├── margin.py          # Margin trading data
 ├── news.py            # News sentiment data
 ├── northbound.py      # Northbound flow data
 ├── pledge.py          # Share pledge data
 ├── cache.py           # Redis cache layer
 └── README.md
 ```

 ## Data sources (priority order)

| Priority | Source | Description |
|----------|--------|-------------|
| 1 | tickflow | Primary source — bulk, fast |
| 2 | Sina | Fallback — real-time quotes |
| 3 | Tencent | Fallback — daily OHLCV |
| 4 | tushare | Fallback — broad coverage |
| 5 | akshare | Last resort — free, rate-limited |

 ## Key interfaces

 ```python
 from data.store import DataStore
 from data.repos.trade_repo import TradeRepo
 from data.repos.factor_repo import FactorRepo

 store = DataStore()
 store.update_daily()

 trade_repo = TradeRepo()
 positions = trade_repo.get_positions(strategy="quant")

 factor_repo = FactorRepo()
 active = factor_repo.get_factors_by_status(("active", "monitoring"))
 ```

 ## Data access rule

 **All SQLite writes go through the Repository layer.** Direct `sqlite3.connect()` outside `data/repos/` is forbidden.
 Read-only queries in `web/app.py` for performance are permitted but must use `data/repos/` connection management.

 ## Related docs

 - [ARCHITECTURE.md — Layer 1](../ARCHITECTURE.md)
 - [Data dictionary](../docs/architecture/DATA_DICTIONARY.md)
 - [Data source catalog](../docs/architecture/DATA-SOURCE-CATALOG.md)
