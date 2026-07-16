"""回测诊断与因子归因模块 — 因子层。

分层:
  - diagnostics.py: 因子层 — compute_pre_backtest_ic() (回测前 IC 评估)
  - analyze.py:     策略层 — FactorTracker / diagnose / apply_diagnosis (回测后分析)
"""

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
import pandas as pd
import numpy as np

_log = get_logger("backtest.diagnostics")


def compute_pre_backtest_ic(factor_names: list, date: str, symbols: list,
                            lookback: int, store=None) -> dict:
    """回测前置 IC — 委托 factor/ic.py 统一计算。

    消除 look-ahead bias：只用 date 之前的数据。
    返回: {factor_name: {"ic_mean": float, "ic_ir": float, "weight": float}}
    """
    from quant.factor.ic import compute_ic as _unified_ic
    result = _unified_ic(factor_names=factor_names, date=date, symbols=symbols,
                         lookback=lookback, store=store, status_filter="backtesting")
    return result.get("ic_map", {})
