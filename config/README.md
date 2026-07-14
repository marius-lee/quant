 # Config Layer (Layer 0)

 Configuration loading, constants, and YAML-based parameter management.

 ## Architecture

 ```
 config/
 ├── loader.py     # YAML config hot-reload (get()) with ${ENV} substitution
 ├── constants.py  # Global factor compute constants (single source of truth)
 ├── config.yaml   # Centralized parameter file
 └── README.md
 ```

 ## Config access pattern

 ```python
 from config.loader import get as cfg
 value = cfg("factor.min_abs_ic", 0.02)
 ```

 For compute constants, use the typed access:

 ```python
 from config.constants import _require_cfg
 window = _require_cfg("factor.windows.amihud")
 ```

 ## Config sections

 - `data` — Data layer parameters
 - `factor` — Factor windows, thresholds, evaluation params
 - `alpha` — Alpha model configuration
 - `risk` — Risk limits and neutralization
 - `optimizer` — Portfolio construction parameters
 - `execution` — Commission, stamp tax, slippage
 - `backtest` — Initial capital, benchmark
 - `web` — Web server port
 - `screening` — Stock screening filters
 - `cache` — Redis/cache configuration
 - `calendar` — Trading calendar settings
 - `attribution` — Attribution model settings
