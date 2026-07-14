 # Web Dashboard (Flask)

 Real-time monitoring dashboard with Flask API backend and threaded scheduler.

 ## Architecture

 ```
 web/
 ├── app.py          # Flask application + scheduler thread + API routes
 ├── shared.py       # Thread-safe in-memory state (SharedState)
 ├── state_broker.py # State broker for cross-thread communication
 ├── state_pusher.py # State push to frontend
 ├── static/         # Frontend assets (JS, CSS)
 └── templates/      # Jinja2 HTML templates
 ```

 ## Running

 ```bash
 cd /Users/mariusto/project/quant
 PYTHONPATH=. python3 web/app.py
 # Open http://localhost:8521
 ```

 ## API endpoints

| Endpoint | Description |
|----------|-------------|
| `/api/state` | Full system state snapshot |
| `/api/performance` | Performance metrics |
| `/api/positions` | Current positions |
| `/api/trades` | Recent trades |
| `/api/signals` | Recent signals |
| `/api/factors` | Active factor list |
| `/api/health` | Health check |

 ## Related docs

 - [ARCHITECTURE.md — Web](../ARCHITECTURE.md)
 - [ADR 008: Frontend real-time architecture](../docs/adr/008-frontend-realtime-architecture.md)
