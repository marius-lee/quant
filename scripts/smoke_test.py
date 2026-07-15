"""冒烟测试 — 快速端到端管线验证（A档：10交易日/300股/60天IC/全因子）。

设计依据 (2026-07-13):
  - 股票数: 300（按流动性取前N，足够覆盖因子计算和组合优化的所有分支）
  - 交易日: ≥10（覆盖至少一个完整双周，验证信号→执行→监控链路）
  - IC窗口: 60天（方向性检查，不做统计推断）
  - 因子池: backtesting 状态池
  - 目的: 只验证管线不崩，不产出投资决策

性能说明 (2026-07-15):
  冒烟测试耗时主要由 backtesting 因子数量决定，不是 primitives 预计算或数据加载。
  - 4 因子时 ≈163s，66 因子时线性膨胀到数十分钟
  - primitives 预计算是固定开销 (~10-20s)，非瓶颈
  - data.lookback_days=365 → 预加载 ~1.2M 行数据，也是固定开销
  因此冒烟测试的因子数必须与"快速验证"的目的匹配：
  - backtesting 因子 ≤8: 冒烟测试合理耗时 (≤5min)
  - backtesting 因子 >20: 不再是冒烟，应降级为正式回测
  如需限制因子数，在 run_backtest 的上游截断:
    factor_names = get_factor_names(status_filter="backtesting")[:8]

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

# A档冒烟测试: 10交易日, 300股, 60天IC
# 因子数 >20 时不是冒烟测试，应在调用前截断 backtesting 因子池到前 8 个
r = run_backtest(
    "2026-06-29", "2026-07-10",   # 10个交易日 (06-29→07-10)
    capital=5000,
    strategy=name,
    universe_size=300,               # A档: 300只
    ic_lookback=60,                  # A档: 60天IC
    factor_status_filter="active",  # 验证目标池; 因子过多时上游做前 N 截断
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
