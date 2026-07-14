 # Market Regime Detection

 Detect market regimes (bull/bear/volatile) to adjust factor weights and risk parameters.

 ## Files

| File | Description |
|------|-------------|
| `detector.py` | HMM-based regime detector |

 ## Usage

 ```python
 from regime.detector import RegimeDetector

 detector = RegimeDetector()
 regime = detector.detect(date)
 # → "bull" | "bear" | "volatile"
 ```

 ## Integration

 Regime information feeds into factor synthesis (regime-specific IC weights) and
 risk management (regime-specific position limits).
