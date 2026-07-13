"""冒烟测试 — 14 交易日快速回测验证端到端流程。

用法:
  PYTHONPATH=. .venv/bin/python3 scripts/smoke_test.py
"""
import sys
sys.path.insert(0, '/Users/mariusto/project/quant')
from backtest.loop import run_backtest

name = "smoke_check"
r = run_backtest("2026-06-20", "2026-07-10", capital=5000, strategy=name)
if "error" in r:
    print(f"ERROR: {r['error']}")
    sys.exit(1)

m = r["metrics"]
print(f"days={m['n_days']}, equity=¥{m['final_equity']:,.0f}, "
      f"sharpe={m['sharpe']:.3f}, cagr={m['cagr_pct']}%, "
      f"mdd={m['max_drawdown_pct']}%, win_rate={m['win_rate']}")
print(f"errors={r['errors']}, elapsed={r['elapsed_sec']:.1f}s")

if r.get("diagnosis"):
    d = r["diagnosis"]
    print(f"diagnosis: {d.get('summary', 'N/A')}")
