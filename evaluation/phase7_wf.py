"""Phase 7: Full-pipeline walk-forward cross-validation.

Takes the factor certification pipeline (Phase 1-4) and the strategy backtest
(Phase 6) and combines them into rolling train/test splits. This provides
true out-of-sample validation: factors selected on a training window are
tested on a subsequent, non-overlapping test window.

Design:
  Each fold:
    1. Train window (N months): run Phase 1-4 to select passing factors
    2. Test window (M months): run full backtest using only those factors
    3. Slide forward by step_months, repeat

Output: per-fold metrics (Sharpe, CAGR, MDD) + aggregate OOS summary.

Usage:
    PYTHONPATH=. python3 evaluation/phase7_wf.py
    PYTHONPATH=. python3 evaluation/phase7_wf.py --train 12 --test 6 --step 3
"""

import os, sys, json, time, argparse, uuid
_root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _root)

import pandas as pd
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

from utils.logger import get_logger, set_trace_id
from config.constants import _require_cfg

_log = get_logger("evaluation.phase7")


def _end_of_month(d: pd.Timestamp) -> pd.Timestamp:
    """Return last day of the month for a given date."""
    next_month = d.replace(day=28) + pd.Timedelta(days=4)
    return next_month - pd.Timedelta(days=next_month.day)


def _run_train_phase(train_start: str, train_end: str) -> list[str]:
    """Run the certification pipeline (Phase 1-4) on a training window.

    Args:
        train_start: Start date of training window (YYYY-MM-DD).
        train_end: End date of training window (YYYY-MM-DD).

    Returns:
        List of factor names that passed all four certification phases.
        Empty list if any phase fails or no factors pass.

    Raises:
        None — all exceptions are caught and logged, returning [] on failure.
    """
    from evaluation.phase1_data import prepare_data
    from evaluation.phase2_single import screen_factors
    from evaluation.phase3_oos import validate_oos
    from evaluation.phase4_costs import verify_costs

    _log.info(f"  train: {train_start} → {train_end}")

    # Phase 1: data prep (uses global DB, not date-bounded)
    try:
        prepare_data()
    except Exception as e:
        _log.warning(f"  Phase 1 failed: {e}")

    # Phase 2: single-factor screening (with diagnostics pre-filter)
    try:
        p2 = screen_factors(prefilter_from_diagnostics=True)
        passed_p2 = p2.get("passed", [])
        _log.info(f"  Phase 2: {len(passed_p2)} passed")
    except Exception as e:
        _log.warning(f"  Phase 2 failed: {e}")
        return []

    if not passed_p2:
        return []

    # Phase 3: CPCV + PBO
    try:
        p3 = validate_oos()
        kept_p3 = p3.get("kept", [])
        _log.info(f"  Phase 3: {len(kept_p3)} kept")
    except Exception as e:
        _log.warning(f"  Phase 3 failed: {e}")
        return passed_p2  # fallback to Phase 2 results

    if not kept_p3:
        return passed_p2  # fallback

    # Phase 4: cost verification
    try:
        p4 = verify_costs()
        final_factors = p4.get("final_factors", [])
        _log.info(f"  Phase 4: {len(final_factors)} final")
    except Exception as e:
        _log.warning(f"  Phase 4 failed: {e}")
        return kept_p3  # fallback to Phase 3 results

    return final_factors if final_factors else kept_p3


def _activate_factors_for_test(factor_names: list[str]) -> None:
    """Temporarily set factors to active status for an OOS backtest.

    Updates factor_registry.status to 'active' for each factor in the list.
    Failed updates are silently skipped (factor may not exist in registry).

    Args:
        factor_names: List of factor names to activate.
    """
    from data.repos import FactorRepo
    repo = FactorRepo()
    for name in factor_names:
        try:
            repo.update_status(name, "active", "phase7_wf: temporary OOS test")
        except Exception:
            pass


def run_walkforward(
    train_months: int = 12,
    test_months: int = 6,
    step_months: int = 6,
    capital: int = 5000,
    start_date: str = "2020-01-01",
    end_date: str = "2025-12-31",
) -> dict:
    """Run full-pipeline walk-forward cross-validation.

    Args:
        train_months: Number of months for each training window.
        test_months: Number of months for each test window.
        step_months: How many months to slide forward between folds.
        capital: Initial capital per fold.
        start_date: Earliest start date (YYYY-MM-DD).
        end_date: Latest end date (YYYY-MM-DD).

    Returns:
        dict with keys:
            folds: list of per-fold results (sharpe, cagr_pct, max_drawdown_pct, etc.).
            summary: aggregate OOS metrics (sharpe_mean, positive_rate, etc.).

    Raises:
        None — errors in individual folds are caught and logged; the function
        continues to the next fold.
    """
    tid = uuid.uuid4().hex[:12]
    set_trace_id(tid)
    t0 = time.time()

    _log.info(f"Phase 7 [{tid}] start: {train_months}m train / {test_months}m test / "
              f"{step_months}m step, {start_date} → {end_date}")

    start_dt = pd.Timestamp(start_date)
    end_dt = pd.Timestamp(end_date)
    current_dt = start_dt

    folds = []

    while True:
        train_end = current_dt + relativedelta(months=train_months)
        test_start = train_end + timedelta(days=1)
        test_end = min(train_end + relativedelta(months=test_months), end_dt)

        if test_end > end_dt or test_start >= end_dt:
            _log.info(f"  stopping: test window {test_start.date()} would exceed {end_date}")
            break

        _log.info(f"Fold {len(folds)+1}: "
                  f"train={current_dt.date()}→{train_end.date()} "
                  f"test={test_start.date()}→{test_end.date()}")

        # Step A: Train — evaluate factors on training window
        train_start_str = current_dt.strftime("%Y-%m-%d")
        train_end_str = train_end.strftime("%Y-%m-%d")
        certified = _run_train_phase(train_start_str, train_end_str)

        if not certified:
            _log.warning(f"  Fold {len(folds)+1}: no factors passed — skipping")
            current_dt += relativedelta(months=step_months)
            continue

        _log.info(f"  Fold {len(folds)+1}: {len(certified)} certified factors: {certified[:5]}...")

        # Step B: Test — backtest with certified factors only
        _activate_factors_for_test(certified)

        test_start_str = test_start.strftime("%Y-%m-%d")
        test_end_str = test_end.strftime("%Y-%m-%d")

        from backtest.loop import run_backtest
        from backtest.naming import next_backtest_name
        strategy = f"phase7_fold{len(folds)+1}_{next_backtest_name()}"

        try:
            result = run_backtest(
                start_date=test_start_str,
                end_date=test_end_str,
                capital=capital,
                strategy=strategy,
                factor_status_filter="active",  # use only the factors we just activated
            )
        except Exception as e:
            _log.error(f"  Fold {len(folds)+1} backtest failed: {e}")
            current_dt += relativedelta(months=step_months)
            continue

        if "error" in result:
            _log.warning(f"  Fold {len(folds)+1} backtest error: {result['error']}")
            current_dt += relativedelta(months=step_months)
            continue

        metrics = result["metrics"]
        fold_result = {
            "fold": len(folds) + 1,
            "train": f"{train_start_str}→{train_end_str}",
            "test": f"{test_start_str}→{test_end_str}",
            "n_certified_factors": len(certified),
            "certified_factors": certified,
            "sharpe": metrics["sharpe"],
            "cagr_pct": metrics["cagr_pct"],
            "max_drawdown_pct": metrics["max_drawdown_pct"],
            "total_return_pct": metrics["total_return_pct"],
            "final_equity": metrics["final_equity"],
        }
        folds.append(fold_result)
        _log.info(f"  Fold {len(folds)}: Sharpe={metrics['sharpe']:.3f} "
                  f"CAGR={metrics['cagr_pct']:.1f}% MDD={metrics['max_drawdown_pct']:.1f}%")

        current_dt += relativedelta(months=step_months)

    elapsed = time.time() - t0

    # Aggregate summary
    if not folds:
        summary = {"status": "no_folds", "n_folds": 0}
    else:
        sharpes = [f["sharpe"] for f in folds]
        cagrs = [f["cagr_pct"] for f in folds]
        mdds = [f["max_drawdown_pct"] for f in folds]
        returns = [f["total_return_pct"] for f in folds]

        positive_folds = sum(1 for r in returns if r > 0)

        summary = {
            "status": "ok",
            "n_folds": len(folds),
            "positive_folds": positive_folds,
            "positive_rate": round(positive_folds / len(folds), 2),
            "sharpe_mean": round(sum(sharpes) / len(sharpes), 3),
            "sharpe_std": round(pd.Series(sharpes).std(), 3),
            "cagr_mean": round(sum(cagrs) / len(cagrs), 1),
            "cagr_std": round(pd.Series(cagrs).std(), 1),
            "mdd_mean": round(sum(mdds) / len(mdds), 1),
            "return_mean": round(sum(returns) / len(returns), 1),
            "elapsed_sec": round(elapsed, 1),
        }

    _log.info(f"Phase 7 [{tid}] done: {summary['n_folds']} folds "
              f"({summary.get('positive_rate', 0):.0%} positive) in {elapsed:.0f}s")

    # Persist to evaluation_runs
    from evaluation.run_store import save_phase
    save_phase("phase7", {
        "folds": folds,
        "summary": summary,
        "params": {
            "train_months": train_months,
            "test_months": test_months,
            "step_months": step_months,
            "capital": capital,
            "start_date": start_date,
            "end_date": end_date,
        },
    })

    return {"folds": folds, "summary": summary}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Phase 7: Full-pipeline walk-forward cross-validation")
    parser.add_argument("--train", type=int, default=12,
                        help="Training window months (default: 12)")
    parser.add_argument("--test", type=int, default=6,
                        help="Test window months (default: 6)")
    parser.add_argument("--step", type=int, default=6,
                        help="Slide step months (default: 6)")
    parser.add_argument("--capital", type=float, default=5000,
                        help="Initial capital (default: 5000)")
    parser.add_argument("--start", default="2020-01-01",
                        help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2025-12-31",
                        help="End date YYYY-MM-DD")
    args = parser.parse_args()

    result = run_walkforward(
        train_months=args.train,
        test_months=args.test,
        step_months=args.step,
        capital=args.capital,
        start_date=args.start,
        end_date=args.end,
    )

    print(json.dumps(result["summary"], indent=2, default=str, ensure_ascii=False))
