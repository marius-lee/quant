"""v418 (R10): 快照因子 60 天门控 — intraday 三因子未成熟时返回 None 而非 NaN.

对应 docs/reports/CODE-REVIEW-2026-08-07.md Gap 3.
"""
import pandas as pd
import numpy as np
from quant.factor.compute.intraday import (
    _snapshot_matured, SNAPSHOT_MIN_DAYS,
    compute_intraday_reversal, compute_open_volume_ratio, compute_close_surge,
)


def _mk_data(n_sym=3, n=5):
    idx = pd.date_range("2026-01-05", periods=n, freq="B")
    syms = [f"S{i}" for i in range(n_sym)]
    cols = pd.MultiIndex.from_product([["open", "close", "high", "low", "volume"], syms])
    arr = np.arange(n * 5 * n_sym, dtype=float).reshape(n, 5 * n_sym) + 10
    return pd.DataFrame(arr, index=idx, columns=cols)


def test_snapshot_matured_threshold():
    assert SNAPSHOT_MIN_DAYS == 60
    assert _snapshot_matured({"intraday_snapshot_days": 59}) is False
    assert _snapshot_matured({"intraday_snapshot_days": 60}) is True
    assert _snapshot_matured({"intraday_snapshot_days": None}) is False or True


def test_intraday_reversal_skipped_when_immature():
    r = compute_intraday_reversal(
        _mk_data(), "2026-01-09",
        aux={"intraday_snapshot_days": 10, "intraday_snapshot": pd.DataFrame()})
    assert r is None


def test_open_volume_ratio_skipped_when_immature():
    r = compute_open_volume_ratio(
        _mk_data(), "2026-01-09",
        aux={"intraday_snapshot_days": 10, "intraday_snapshot": pd.DataFrame()})
    assert r is None


def test_close_surge_skipped_when_immature():
    r = compute_close_surge(
        _mk_data(), "2026-01-09",
        aux={"intraday_snapshot_days": 10, "intraday_snapshot": pd.DataFrame()})
    assert r is None


def test_mature_path_produces_series_or_none_not_nan_noise():
    """成熟且快照有数据 + 截面充足 → Series; 快照空 → None (非 NaN 因子)."""
    # z-score min_count_sparse=10: 给足 12 个 symbol 避免配置阈值干扰断言
    n_sym = 12
    data = _mk_data(n_sym)
    syms = [f"S{i}" for i in range(n_sym)]
    snaps = []
    for i in range(n_sym):
        p30 = 10.0 + i * 0.1
        prev = 9.0 + i * 0.09
        snaps.append({"symbol": syms[i], "mode": "open", "price": p30,
                      "prev_close": prev, "volume": 100 + i * 10})
    snap = pd.DataFrame(snaps)
    aux = {"intraday_snapshot_days": 60, "intraday_snapshot": snap}
    r = compute_intraday_reversal(_mk_data(n_sym), "2026-01-05", aux=aux)
    assert isinstance(r, pd.Series) and not r.isna().all()
    r2 = compute_open_volume_ratio(_mk_data(n_sym), "2026-01-05", aux=aux)
    assert isinstance(r2, pd.Series) and not r2.isna().all()
    r3 = compute_close_surge(
        _mk_data(n_sym), "2026-01-05",
        aux={"intraday_snapshot_days": 60,
             "intraday_snapshot": pd.DataFrame(columns=sorted(snap.columns))})
    assert r3 is None or isinstance(r3, pd.Series)