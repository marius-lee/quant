"""回归测试: curator 表达式因子必须接入物化池并能真算 (V570 任务1).

防回归点:
  - get_factor_names('backtesting') 须含 7 个表达式因子 (amihud_proxy 等),
    否则 load_active_* 的表达式接入被回退 → 物化漏算.
  - compute_all_factors 按 registry compute_fn 编译公式, 须产出非 NaN 序列,
    否则 compute_fn 被写成自引用占位 (data['xxx'] 列不存在 → 全 NaN).
"""
import pandas as pd
import numpy as np
import pytest

from quant.factor.compute import get_factor_names
from quant.factor.compute._dispatch import compute_all_factors

EXPR7 = [
    "amihud_proxy", "micro_gap", "money_flow_cmf", "residual_momentum_proxy",
    "volume_price_trend", "wq_alpha_001", "wq_alpha_032",
]


def _pool():
    return set(get_factor_names("backtesting")) | set(get_factor_names("using"))


def test_expr_factors_in_materialization_pool():
    pool = _pool()
    missing = [f for f in EXPR7 if f not in pool]
    assert not missing, f"表达式因子漏出物化池: {missing}"


def _synthetic_data(n_days=480, n_sym=80):
    dates = pd.date_range("2023-01-01", periods=n_days, freq="B")
    cols = pd.MultiIndex.from_product(
        [["close", "open", "high", "low", "volume"],
         [f"S{i:03d}" for i in range(n_sym)]])
    rng = np.random.default_rng(0)
    data = pd.DataFrame(rng.normal(size=(n_days, 5 * n_sym)), index=dates, columns=cols).abs() + 1
    data["volume"] *= 1e6
    return data, str(dates[-1].date())


def test_expr_factors_compute_non_nan():
    data, d = _synthetic_data()
    res = compute_all_factors(data, d, factor_names=EXPR7,
                              status_filter="backtesting", quiet=True)
    bad = [k for k in EXPR7 if not (isinstance(res.get(k), pd.Series) and res[k].notna().any())]
    assert not bad, f"表达式因子全 NaN (compute_fn 接线坏): {bad}"
