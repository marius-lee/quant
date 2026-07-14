 # Alpha Layer (Layer 3)

 Synthesizes multiple factors into a single alpha vector (expected return) and performs cross-sectional ranking.

 ## Architecture

 ```
 alpha/
 ├── model.py     # AlphaModel — factor synthesis + return prediction + cross-sectional ranking
 ├── synth.py     # Alpha synthesis variants
 ├── multi_tf.py  # Multi-timeframe alpha blending
 ├── rotation.py  # Sector rotation logic
 └── README.md
 ```

 ## Key interface

 ```python
 from alpha.model import AlphaModel

 model = AlphaModel(factors, method="ic_weighted")
 model.calibrate(factor_values, forward_returns)
 alpha = model.predict(date, store)      # Series[symbol → score]
 ranked = model.cross_sectional_rank(alpha)  # Series[symbol → percentile]
 ```

 ## Synthesis methods

| Method | Description |
|--------|-------------|
| `ic_weighted` | Weight factors by trailing IC, decay-weighted |
| `equal_weight` | Simple average of standardized factor values |
| `machine_learning` | LightGBM/XGBoost trained on past factor → return mapping |

 ## Related docs

 - [ARCHITECTURE.md — Layer 3](../ARCHITECTURE.md)
