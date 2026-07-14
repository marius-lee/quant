 # Factor Index Cards

 Structured JSON per factor — the canonical source of truth for factor metadata.

 ## Schema

 ```json
 {
   "name": "momentum_63d",
   "display_name": "63日动量",
   "category": "momentum",
   "sub_category": "price",
   "formula": "close / close.shift(63) - 1",
   "window_days": 63,
   "data_deps": ["daily.close"],
   "reference": "Jegadeesh & Titman (1993)",
   "hypothesis": "why we think this factor works",
   "status": "active",
   "status_history": [
     {"from": "registered", "to": "candidate", "date": "2025-01-01", "reason": "Initial registration"},
     {"from": "candidate", "to": "active", "date": "2025-03-01", "reason": "IC > 0.02, t > 2.0"}
   ],
   "ic_mean_12m": 0.034,
   "ic_std_12m": 0.12,
   "icir_12m": 0.28,
   "half_life_days": 18,
   "decay_trend": "stable|declining|improving",
   "correlations": {"related_factor": 0.72},
   "market_regime_performance": {
     "bull": {"ic_mean": 0.045},
     "bear": {"ic_mean": 0.012}
   },
   "last_evaluated": "2026-07-10"
 }
 ```

 ## Auto-generation

 Cards are updated automatically after each evaluation cycle by `factor/ic.py`,
 which reads the latest IC statistics and writes updated JSON.

 To manually generate cards for all registered factors:

 ```bash
 PYTHONPATH=. python3 scripts/generate_factor_cards.py
 ```

 ## Relationship to factor_registry

 Cards are **complementary to** the DB-based `factor_registry`:

| Registry (DB) | Cards (JSON) |
|---------------|-------------|
| Runtime state machine | Persistent knowledge |
| Status transitions | History + rationale |
| IC scores (latest) | IC time series + market regime breakdown |
| Active/monitoring/retired flags | Hypotheses + evidence |

 Cards are human-readable and version-controlled. The registry is the runtime truth.
