 # Scripts

 Operational scripts for data initialization, factor registration, validation, and maintenance.

 ## Scripts

| Script | Purpose |
|--------|---------|
| `init_data.py` | Initialize database schema and seed data |
| `rebuild_factor_cache.py` | Rebuild factor computation cache |
| `register_momentum_variants.py` | Register momentum factor variants |
| `activate_candidates.py` | Promote candidate factors to active |
| `fix_daily_data.py` | Fix daily data anomalies |
| `fix_formatting.py` | Auto-format Python files |
| `run_task.py` | Generic task runner |
| `validate.py` | System validation checks |
| `smoke_test.py` | Quick smoke test of core pipeline |
| `test_cache_integration.py` | Cache integration test |

 ## Evaluation scripts (not in this directory)

 ```bash
 bash scripts/eval_layer12.sh       # L1+L2 fast evaluation
 bash scripts/eval_stepwise.sh      # L1+L2+L3 full evaluation
 bash scripts/eval_standard.sh      # Five-phase standard evaluation
 ```

 ## Usage

 ```bash
 cd /Users/mariusto/project/quant
 PYTHONPATH=. python3 scripts/rebuild_factor_cache.py
 ```
