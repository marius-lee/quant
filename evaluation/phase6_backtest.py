"""Phase 6: Strategy-level walk-forward backtest.

Integrates the full pipeline (factors → alpha → risk → portfolio → execution)
into the 5-phase evaluation framework. Runs a day-by-day simulation with
realistic constraints (T+1, commissions, lot sizes, stop-loss).

Placement: After Phase 3 (OOS validated factors) and Phase 4 (cost check).
Input: factors that passed Phase 3 (candidate + active).
Output: equity curve, Sharpe, MDD, CAGR, benchmark delta, cost breakdown.

Usage:
    PYTHONPATH=. python3 evaluation/phase6_backtest.py
    PYTHONPATH=. python3 evaluation/phase6_backtest.py --start 2022-01-01 --end 2024-12-31
"""

import os, sys, json, time, argparse
_root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _root)

from utils.logger import get_logger
_log = get_logger("evaluation.phase6")


def run_strategy_backtest(
    start_date="2023-01-01",
    end_date="2025-12-31",
    capital=5000,
    output_json=None,
    strategy=None,
) -> dict:
    """Run strategy-level backtest and return results.

    Returns dict compatible with evaluation_runs DB storage.
    """
    from backtest.loop import run_backtest
    from evaluation.run_store import save_phase

    _log.info(f"Phase 6 start: {start_date} → {end_date}, capital=Y{capital:,}")

    t0 = time.time()
    from backtest.naming import next_backtest_name
    if strategy is None:
        strategy = next_backtest_name()

    result = run_backtest(
        start_date=start_date,
        end_date=end_date,
        capital=capital,
        strategy=strategy or "phase6",
    )

    elapsed = time.time() - t0

    if "error" in result:
        _log.error(f"Phase 6 failed: {result['error']}")
        return {"status": "failed", "error": result["error"]}

    metrics = result["metrics"]
    n_days = metrics["n_days"]

    phase6_data = {
        "status": "ok",
        "start_date": start_date,
        "end_date": end_date,
        "initial_capital": capital,
        "final_equity": metrics["final_equity"],
        "total_return_pct": metrics["total_return_pct"],
        "cagr_pct": metrics["cagr_pct"],
        "sharpe": metrics["sharpe"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "win_rate": metrics["win_rate"],
        "n_trading_days": n_days,
        "errors": result["errors"],
        "avg_signals_per_day": result["avg_signals_per_day"],
        "elapsed_sec": round(elapsed, 1),
    }

    # Persist
    try:
        rid = save_phase("phase6", phase6_data)
        _log.info(f"Phase 6 saved: run_id={rid}")
    except Exception as e:
        _log.warning(f"Phase 6: save failed (non-fatal): {e}")

    # Optional JSON output
    if output_json:
        with open(output_json, "w") as f:
            json.dump(phase6_data, f, indent=2, default=str, ensure_ascii=False)

    _log.info(
        f"Phase 6 complete ({elapsed:.0f}s): "
        f"return={metrics['total_return_pct']}%, "
        f"Sharpe={metrics['sharpe']}, "
        f"MDD={metrics['max_drawdown_pct']}%"
    )
    return phase6_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 6: Strategy-level backtest")
    parser.add_argument("--start", default="2023-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2025-12-31", help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=5000, help="Initial capital")
    parser.add_argument("--output", default="/tmp/_eval_phase6.json", help="Output JSON path")
    args = parser.parse_args()

    result = run_strategy_backtest(
        start_date=args.start,
        end_date=args.end,
        capital=args.capital,
        output_json=args.output,
    )
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
