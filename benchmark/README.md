 # Benchmark

 Benchmark tracking — compare strategy performance against market indices (CSI 300, CSI 500).

 ## Files

| File | Description |
|------|-------------|
| `tracker.py` | Benchmark tracker — daily NAV comparison |

 ## Usage

 ```python
 from benchmark.tracker import BenchmarkTracker

 tracker = BenchmarkTracker(benchmark="000300.SH")
 tracker.update(date, strategy_nav)
 excess = tracker.excess_return()
 ```
