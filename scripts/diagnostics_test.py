"""回测诊断 — 因子快照（backtesting 因子池的 IC 计算 + 诊断）。

与冒烟测试的区别:
  - 冒烟测试用 active 因子 (2个) → 验证管线
  - 诊断用 backtesting 因子 (66个) → 因子评估 + 状态变更建议

用法:
  PYTHONPATH=. .venv/bin/python scripts/diagnostics_test.py
"""
import sys
import os as _os; sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from quant.backtest.loop import run_backtest
from quant.backtest.naming import next_backtest_name
from quant.utils.excepthook import setup; setup()

name = next_backtest_name()
print(f"[diagnostics] strategy={name}")

r = run_backtest(
    "2026-06-01", "2026-06-30",
    capital=5000,
    strategy=name,
    universe_size=300,
    ic_lookback=60,
    factor_status_filter="backtesting",
)

if "error" in r:
    print(f"ERROR: {r['error']}")
    sys.exit(1)

m = r["metrics"]
print(f"days={m['n_days']}, sharpe={m['sharpe']}, cagr={m['cagr_pct']}%, "
      f"mdd={m['max_drawdown_pct']}%, elapsed={r['elapsed_sec']}s")
if r.get("diagnosis"):
    print(f"diagnosis: {r['diagnosis']['summary']}")
