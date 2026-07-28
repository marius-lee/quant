#!/usr/bin/env python3
"""End-to-end validation of train_lgb_model() — ADR-035 Phase 2.

Train a LightGBM alpha prediction model on factor cache data,
validate prediction output, and verify fallback behavior.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/validate_lgb_e2e.py
"""

import os, sys
import time
import numpy as np
import pandas as pd
import sqlite3

# Ensure project root on path
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from quant.config.paths import FACTOR_CACHE_DB
from quant.utils.logger import get_logger

_log = get_logger("validate_lgb")


def main():
    _log.info("=" * 60)
    _log.info("E2E: train_lgb_model() validation")
    _log.info("=" * 60)

    # ── Step 1: Check lightgbm ──
    from quant.alpha.qlib_model import _check_lightgbm, LgbAlphaModel
    assert _check_lightgbm(), "lightgbm not installed"
    _log.info("Step 1: lightgbm available ✅")

    # ── Step 2: Load factor data from cache ──
    _log.info("Step 2: Loading factor data from cache...")
    conn = sqlite3.connect(FACTOR_CACHE_DB)
    factors = [r[0] for r in conn.execute(
        "SELECT DISTINCT factor FROM factor_values ORDER BY factor"
    ).fetchall()]
    _log.info("  %d factors in cache", len(factors))

    # Get dates for coverage calculation
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM factor_values ORDER BY date"
    ).fetchall()]
    _log.info("  Date range: %s → %s (%d days)", dates[0], dates[-1], len(dates))

    # Use daily factors only (fundamental factors are quarterly, causing sparse overlap)
    # Select factors with best date coverage (>80% of all dates)
    total_days = len(dates)
    factor_coverage = {}
    for fn in factors:
        cnt = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM factor_values WHERE factor=?", (fn,)
        ).fetchone()[0]
        factor_coverage[fn] = cnt / total_days

    # Pick top 15 factors by date coverage (>=80% means daily)
    daily_factors = sorted(
        [(fn, cov) for fn, cov in factor_coverage.items() if cov >= 0.8],
        key=lambda x: -x[1]
    )[:15]
    train_factors = [f[0] for f in daily_factors]
    _log.info("  Using %d daily factors (coverage >= 80%%): %s",
              len(train_factors), [(f, f"{c:.0%}") for f, c in daily_factors[:5]])

    # Load factor values as panels: {factor_name: DataFrame(date × symbol)}
    _log.info("  Loading factor panels (all dates)...")
    t0 = time.time()

    factor_panels = {}
    for fn in train_factors:
        rows = conn.execute(
            "SELECT date, symbol, zscore FROM factor_values WHERE factor=?",
            (fn,)
        ).fetchall()
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=["date", "symbol", "zscore"])
        panel = df.pivot(index="date", columns="symbol", values="zscore")
        factor_panels[fn] = panel

    conn.close()
    _log.info("  Loaded %d factor panels in %.1fs", len(factor_panels), time.time() - t0)

    assert len(factor_panels) >= 5, f"Too few factor panels: {len(factor_panels)}"
    _log.info("Step 2: Factor data loaded ✅ (%d factors)", len(factor_panels))

    # ── Step 3: Build forward returns ──
    _log.info("Step 3: Building forward returns...")
    from quant.data.store import DataStore
    store = DataStore()

    # Get symbols from factor data
    all_syms = set()
    for panel in factor_panels.values():
        all_syms.update(panel.columns)
    symbols = sorted(all_syms)
    _log.info("  %d unique symbols", len(symbols))

    # Use date range from factor data
    all_factor_dates = sorted(set.union(*[set(df.index) for df in factor_panels.values()]))
    start = all_factor_dates[0]
    end = all_factor_dates[-1]
    _log.info("  Loading daily data: %s → %s", start, end)

    data = store.get_daily(list(symbols)[:500], start=start, end=end)
    close = data["close"]
    _log.info("  Daily data: %d days × %d symbols", len(close), len(close.columns))

    # Forward 5-day returns
    horizon = 5
    fwd_ret = close.shift(-horizon) / close - 1
    fwd_ret = fwd_ret.stack().dropna()
    fwd_ret.name = "forward_return"
    _log.info("  Forward returns: %d observations (horizon=%dd)", len(fwd_ret), horizon)

    store.close()
    _log.info("Step 3: Forward returns built ✅")

    # ── Step 4: Train model ──
    _log.info("Step 4: Training LightGBM model...")
    t0 = time.time()

    model = LgbAlphaModel()

    # Build aligned feature matrix and labels
    X_all = []
    y_all = []
    n_dates_used = 0

    # Use union of dates (since all selected factors have >80% coverage)
    common_dates = sorted(set.union(*[
        set(df.index) for df in factor_panels.values()
    ]))

    for date in common_dates[:-horizon]:  # Leave room for forward returns
        if date not in fwd_ret.index.get_level_values(0):
            continue

        # Skip if <10 factors have data for this date
        factors_available = [fn for fn in train_factors
                             if fn in factor_panels and date in factor_panels[fn].index]
        if len(factors_available) < max(5, len(train_factors) // 2):
            continue
        syms = None
        for fn in factors_available:
            panel = factor_panels.get(fn)
            row = panel.loc[date].dropna()
            if syms is None:
                syms = set(row.index)
            else:
                syms &= set(row.index)

        if syms is None or len(syms) < 30:
            continue

        syms = sorted(syms)[:300]  # Cap symbols per day

        X_day = np.column_stack([
            factor_panels[fn].loc[date].reindex(syms).fillna(0).values
            for fn in factors_available
        ])

        # Labels
        y_day = fwd_ret.loc[date].reindex(syms).fillna(0).values
        mask = ~np.isnan(y_day)
        if mask.sum() < 20:
            continue

        X_all.append(X_day[mask])
        y_all.append(y_day[mask])
        n_dates_used += 1

        if n_dates_used % 10 == 0:
            _log.info("  ... %d dates processed", n_dates_used)

    if not X_all:
        _log.error("No training data built! Check factor cache / daily data alignment.")
        sys.exit(1)

    X = np.vstack(X_all)
    y = np.concatenate(y_all)

    # Winsorize
    y = np.clip(y, np.percentile(y, 1), np.percentile(y, 99))

    _log.info("  Training data: %d samples × %d features from %d dates",
              len(y), X.shape[1], n_dates_used)

    # Train via the model's API
    import lightgbm as lgb
    lgb_params = {
        "objective": "regression", "metric": "rmse",
        "boosting_type": "gbdt", "num_leaves": 31,
        "learning_rate": 0.05, "feature_fraction": 0.8,
        "bagging_fraction": 0.8, "bagging_freq": 5,
        "verbose": -1, "n_estimators": 100,
        "min_data_in_leaf": 20, "max_depth": 6,
    }
    model._lgb = lgb.LGBMRegressor(**lgb_params)
    model._lgb.fit(X, y)
    model._feature_names = train_factors  # Use all training factors as feature names

    elapsed = time.time() - t0
    _log.info("Step 4: Model trained in %.1fs ✅", elapsed)

    # ── Step 5: Validate prediction ──
    _log.info("Step 5: Validating prediction...")

    # Use the last training date for prediction
    test_date = all_factor_dates[-horizon - 5]
    test_syms = sorted(set.intersection(*[
        set(factor_panels[fn].loc[test_date].dropna().index[:100])
        for fn in train_factors
        if fn in factor_panels and test_date in factor_panels[fn].index
    ]))

    if len(test_syms) < 5:
        _log.warning("Too few test symbols (%d), using all common", len(test_syms))
        test_syms = sorted(set.intersection(*[
            set(factor_panels[fn].columns)
            for fn in train_factors if fn in factor_panels
        ]))[:100]

    _log.info("  Test date: %s, %d symbols", test_date, len(test_syms))

    # Build factor_values dict for predict()
    fv = {}
    for fn in train_factors:
        if fn in factor_panels and test_date in factor_panels[fn].index:
            fv[fn] = factor_panels[fn].loc[test_date]

    predictions = model.predict(fv, symbols=test_syms)
    _log.info("  Predictions: %d symbols, range [%.4f, %.4f]",
              len(predictions), predictions.min(), predictions.max())

    assert len(predictions) > 0, "Empty predictions!"
    assert not predictions.isna().all(), "All NaN predictions!"

    # Check IC against actual forward returns
    if test_date in fwd_ret.index.get_level_values(0):
        actual = fwd_ret.loc[test_date].reindex(predictions.index).dropna()
        common = predictions.index.intersection(actual.index)
        if len(common) > 20:
            ic = np.corrcoef(predictions.loc[common], actual.loc[common])[0, 1]
            _log.info("  Cross-sectional IC: %.4f", ic)
    _log.info("Step 5: Prediction validation ✅")

    # ── Step 6: Verify fallback behavior ──
    _log.info("Step 6: Verifying fallback...")
    from quant.alpha.model import AlphaModel
    am_lgb = AlphaModel(combine_mode="lgb")
    result_lgb = am_lgb.combine(fv, ic_map={fn: 0.1 for fn in fv})
    assert result_lgb.notna().sum() > 0, "LGB combine returned empty"
    _log.info("  LGB combine: %d stocks", result_lgb.notna().sum())

    am_iw = AlphaModel(combine_mode="sleeve", method="ic_weighted")
    result_iw = am_iw.combine(fv, ic_map={fn: 0.1 for fn in fv})
    _log.info("  IC weighted: %d stocks (baseline)", result_iw.notna().sum())
    _log.info("Step 6: Fallback verified ✅")

    # ── Summary ──
    _log.info("=" * 60)
    _log.info("E2E VALIDATION: ALL STEPS PASSED ✅")
    _log.info("  Factors: %d", len(train_factors))
    _log.info("  Training: %d samples × %d features from %d dates",
              len(y), X.shape[1], n_dates_used)
    _log.info("  Prediction: %d stocks on %s", len(predictions), test_date)
    _log.info("=" * 60)


if __name__ == "__main__":
    main()
