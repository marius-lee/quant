 # Tests

 pytest-based test suite. 67 tests covering factor computation, portfolio construction,
 execution, risk constraints, and factor registry.

 ## Running

 ```bash
 # All tests
 pytest -v

 # Specific module
 pytest tests/test_factor_compute.py -v

 # With coverage
 pytest --cov=. --cov-report=html

 # Marked tests
 pytest -m "not slow"
 ```

 ## Test markers

| Marker | Description |
|--------|-------------|
| `unit` | Fast, no external dependencies |
| `integration` | Requires market.db |
| `slow` | Long-running (IC computation, backtest) |

 ## Test files

| File | Coverage |
|------|----------|
| `test_factor_compute.py` | Factor computation functions |
| `test_constraints.py` | Risk constraints |
| `test_execution.py` | Execution engine |
| `test_portfolio.py` | Portfolio construction |
| `test_synth.py` | Factor synthesis |
| `test_registry_smoke.py` | Factor registry |
| `test_marginal.py` | Marginal contribution |
| `conftest.py` | Shared fixtures |
