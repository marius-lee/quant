 # Installation

 ## Prerequisites

 - Python 3.12+
 - SQLite 3 (built-in)
 - Git

 ## Setup

 ```bash
 cd /Users/mariusto/project/quant

 # Create virtual environment
 python3 -m venv .venv
 source .venv/bin/activate

 # Install with dev dependencies
 pip install -e ".[dev]"
 ```

 ## Data initialization

 ```bash
 # Initialize database schema
 PYTHONPATH=. python3 scripts/init_data.py

 # Sync daily data (first run: ~30 min for full A-share history)
 PYTHONPATH=. python3 daily_sync.py
 ```

 ## Verify

 ```bash
 # Run tests
 pytest -v

 # Check factor computation
 PYTHONPATH=. python3 -c "from factor.compute._registry import get_factor_names; print(len(get_factor_names()), 'factors')"

 # Start web service
 PYTHONPATH=. python3 web/app.py
 # → http://localhost:8521
 ```
