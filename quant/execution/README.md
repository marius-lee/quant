 # Execution Layer (Layer 6)

 Simulated trading engine with unified cost model and real-time market quotes.

 ## Architecture

 ```
 execution/
 ├── engine.py       # ExecutionEngine — order recording, position tracking
 ├── cost.py         # CostModel — commission + stamp tax + slippage
 ├── quote.py        # fetch_quotes() — Sina real-time batch quotes
 ├── calendar.py     # A-share trading calendar
 ├── impact.py       # Market impact estimation
 ├── stop_loss.py    # Stop-loss logic
 └── README.md
 ```

 ## Key interfaces

 ```python
 from execution.cost import CostModel
 from execution.engine import ExecutionEngine

 cost = CostModel()
 engine = ExecutionEngine()

 engine.execute(orders, date="2026-07-14", strategy="quant")
 ```

 ## Cost model

| Component | Rate | Notes |
|-----------|------|-------|
| Commission | 0.03% | Min ¥5/order |
| Stamp tax | 0.10% | Sell only |
| Slippage | 0.10% | Both directions |

 ## Related docs

 - [ARCHITECTURE.md — Layer 6](../ARCHITECTURE.md)
