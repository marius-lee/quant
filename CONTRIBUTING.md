 # Contributing

 We welcome contributions and suggestions. Whether it's fixing a bug, adding a factor, improving documentation, or correcting a typo — every contribution is valuable.

 ## Getting started

 ```bash
 cd /Users/mariusto/project/quant
 
 # Install in editable mode with dev deps
 pip install -e ".[dev]"

 # Run tests
 pytest -v

 # Run linting
 ruff check .
 ```

 ## How to contribute

 1. Create a feature branch from `main`
    ```bash
    git checkout -b feature/your-feature-name
    ```
 2. Make your changes
 3. Run tests and linting
    ```bash
    pytest -v && ruff check .
    ```
 4. Commit with a descriptive message
    ```bash
    git commit -m "factor: add turnover_anomaly_21d"
    ```
 5. Push and create a pull request

 ## Commit conventions

 Prefix your commit with the module name:

 | Prefix | Module |
 |--------|--------|
 | `factor:` | Factor computation / evaluation / registry |
 | `backtest:` | Backtest engine / diagnostics |
 | `eval:` | Evaluation pipeline (phases 1-7) |
 | `exec:` | Execution / cost model / calendar |
 | `optimizer:` | Portfolio construction / rebalance |
 | `risk:` | Neutralization / covariance / constraints |
 | `alpha:` | Alpha model / factor synthesis |
 | `data:` | Data store / repos / sync |
 | `web:` | Flask API / frontend |
 | `monitor:` | Attribution / reports / alerts |
 | `config:` | Configuration / constants |
 | `docs:` | Documentation |
 | `test:` | Tests |
 | `infra:` | CI / tooling / pyproject.toml |

 ## Code standards

 - Configuration-driven: all thresholds/parameters from `config.yaml`, no hardcoded magic numbers
 - Vectorized factor computation: use pandas/numpy, avoid Python loops over stocks
 - Log through `utils.logger.get_logger`, never use `print()` in library code
 - SQLite access through `data.repos.*`, never raw `sqlite3.connect()` outside repos
 - Follow the 7-layer architecture: lower layers never import from higher layers

 ## Adding a new factor

 1. Implement the compute function in `factor/compute/price/` or `factor/compute/fundamental.py`
 2. Register it in the `_PRICE_FN_MAP` or `_FUNDAMENTAL_FN_MAP`
 3. Add parameters to `config/config.yaml` under `factor.`
 4. Add a factor card at `factor/cards/{factor_name}.json`
 5. Run `scripts/eval_standard.sh` to validate through the evaluation pipeline

 ## Reporting issues

 Open an issue with:
 - Steps to reproduce
 - Expected vs actual behavior
 - Relevant log output from `logs/quant.log`
 - Environment (Python version, OS)

 ## Code of conduct

 Be respectful. Focus on the code and the problem. Assume good intent.
