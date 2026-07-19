"""冒烟测试 — 快速端到端管线验证（A档：10交易日/300股/60天IC/active因子）。

设计依据 (2026-07-13, 更新 2026-07-19):
  - 股票数: 300（按流动性取前N，足够覆盖因子计算和组合优化的所有分支）
  - 交易日: ≥10（覆盖至少一个完整双周，验证信号→执行→监控链路）
  - IC窗口: 60天（方向性检查，不做统计推断）
  - 因子池: 使用 active 状态因子, 快速验证评估业务流程是否通畅
  - 目的: 只验证管线不崩，不产出投资决策

性能说明 (2026-07-19):
  与正式评估 (backtesting 因子) 职责分离 — 冒烟验证链路，正式评估验证因子质量。

用法:
  PYTHONPATH=. .venv/bin/python3 scripts/smoke_test.py

相关配置:
  - backtest.universe_size: 300  (冒烟覆盖值)
  - backtest.diagnosis_ic_window: 60  (冒烟覆盖值)
  - backtest.min_trading_days: 10
"""
import sys
import os as _os; sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from quant.backtest.loop import run_backtest
from quant.backtest.naming import next_smoke_name
from quant.utils.excepthook import setup; setup()  # crash → app.log (after logger init)

name = next_smoke_name()  # smoke_1, smoke_2, ...
print(f"[smoke] strategy={name}")

# A档冒烟测试: 10交易日, 300股, 60天IC, active因子
r = run_backtest(
    "2026-06-29", "2026-07-10",   # 10个交易日 (06-29→07-10)
    capital=5000,
    strategy=name,
    universe_size=300,               # A档: 300只
    ic_lookback=60,                  # A档: 60天IC
    factor_status_filter="active",  # 验证目标池: active因子
)

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
