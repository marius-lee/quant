 # Configuration

 All parameters are centralized in `config/config.yaml`. No hardcoded magic numbers.

 ## Access pattern

 ```python
 from config.loader import get as cfg
 value = cfg("factor.min_abs_ic", 0.02)
 ```

 ## Environment substitution

 Config values can reference environment variables:

 ```yaml
 web:
   host: ${WEB_HOST:-127.0.0.1}
   port: ${WEB_PORT:-8521}
 ```

 ## Key sections

 | Section | Description | Key params |
 |---------|-------------|------------|
 | `data` | Data layer | `sqlite.timeout`, `sync.batch_size` |
 | `factor` | Factor configuration | `windows.*`, `evaluation.*`, `compute.*` |
 | `alpha` | Alpha model | `method`, `train_window`, `retrain_freq` |
 | `risk` | Risk limits | `max_single_position`, `max_positions`, `covariance_method` |
 | `optimizer` | Portfolio construction | `nano_cap`, `micro_cap`, `kelly_fraction`, `rebalance_freq` |
 | `execution` | Cost model | `commission`, `stamp_tax`, `slippage` |
 | `backtest` | Backtest params | `initial_capital`, `benchmark` |
 | `web` | Web server | `host`, `port`, `debug` |

 ## Hot-reload

 Config is reloaded automatically on each `get()` call. No restart needed.
 ```python
 from config.loader import reload
 reload()  # Force re-read from disk
 ```
