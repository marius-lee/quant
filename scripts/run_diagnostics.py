"""回测诊断 — 因子快照（backtesting 因子池的 IC 计算 + 诊断）。

与冒烟测试的区别:
  - 冒烟测试用 active 因子 (2个) → 验证管线
  - 诊断用 backtesting 因子 → 因子 IC 快照 + 状态评估

用法:
  PYTHONPATH=. .venv/bin/python scripts/run_diagnostics.py
"""
import sys
import os as _os; sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from quant.utils.excepthook import setup; setup()
from quant.utils.logger import get_logger, set_trace_id
from quant.backtest.diagnostics import compute_pre_backtest_ic
from quant.factor.compute._registry import get_factor_names
from quant.data.store import DataStore
from quant.config.constants import _require_cfg
import uuid as _uuid

tid = _uuid.uuid4().hex[:12]
set_trace_id(tid)
log = get_logger("backtest.diagnostics")

log.info("=" * 70)
log.info("  DIAGNOSTICS START: factor IC snapshot")
log.info("=" * 70)

store = DataStore()
factors = get_factor_names(status_filter="backtesting")
log.info(f"backtesting factors: {len(factors)}")

# 取流动性前 2000 只
try:
    from quant.data.store import market_conn
    symbols = [r[0] for r in market_conn().execute('SELECT symbol FROM stocks LIMIT 6000').fetchall()]
except Exception:
    from quant.data.store import market_conn
    symbols = [r[0] for r in market_conn().execute(
        "SELECT symbol FROM stocks ORDER BY symbol LIMIT 2000"
    ).fetchall()]
log.info(f"symbols: {len(symbols)}")

ic_map = compute_pre_backtest_ic(factors, "2026-07-01", symbols, lookback=120, store=store)
log.info(f"IC computed: {len(ic_map)} factors")

# 分类
strong = {k: v for k, v in ic_map.items() if abs(v.get("ic_mean", 0)) >= 0.03}
moderate = {k: v for k, v in ic_map.items() if 0.02 <= abs(v.get("ic_mean", 0)) < 0.03}
weak = {k: v for k, v in ic_map.items() if abs(v.get("ic_mean", 0)) < 0.02}

log.info(f"Strong  (|IC|>=0.03): {len(strong)}")
log.info(f"Moderate (|IC| 0.02-0.03): {len(moderate)}")
log.info(f"Weak    (|IC|<0.02): {len(weak)}")

log.info("--- Top 10 by |IC| ---")
for k, v in sorted(ic_map.items(), key=lambda x: abs(x[1].get("ic_mean", 0)), reverse=True)[:10]:
    log.info(f"  {k}: IC={v['ic_mean']:.4f}, IR={v['ic_ir']:.2f}")

log.info("=" * 70)
log.info(f"  DIAGNOSTICS END: {len(ic_map)} factors evaluated")
log.info("=" * 70)

print(f"\n[diagnostics] done: {len(ic_map)} factors")
print(f"  Strong:  {len(strong)}")
print(f"  Moderate: {len(moderate)}")
print(f"  Weak:    {len(weak)}")
