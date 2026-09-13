"""Pipeline run."""
def run(date_str: str = None, capital: float = None, strategy: str = "quant", skip_pull: bool = False):
    """完整 Pipeline（向后兼容包装器）。

    阶段一: generate_signals() → 目标持仓
    阶段二: execute_signals() → 执行交易
    """
    signals = generate_signals(date_str, capital, strategy, skip_pull)
    if "target_positions" not in signals:
        logger.warning("generate_signals returned no target positions, skipping execution")
        return signals

    exec_result = execute_signals(signals["target_positions"], signals["date"], strategy)
    # Merge steps
    signals["steps"].update(exec_result.get("steps", {}))
    signals["elapsed_sec"] = signals.get("elapsed_sec", 0) + exec_result.get("elapsed_sec", 0)
    signals["stopped_out"] = exec_result.get("stopped_out", [])
# ── Trace recording (non-blocking) ──
    try:
        from quant.core.trace import get_trace, make_experiment, Hypothesis, ExperimentFeedback
        trace = get_trace()
        exp = make_experiment(
            action="pipeline_run",
            hypothesis=Hypothesis(
                hypothesis=f"Strategy {strategy} generates excess returns",
                reason=f"Pipeline run: factor eval + execution",
                source="pipeline.run()",
            ),
        )
        steps = signals.get("steps", {})
        exp.sub_results = {"date": signals.get("date"), "elapsed_sec": signals.get("elapsed_sec", 0)}
        exp.sub_results["steps_summary"] = {k: {sk: sv for sk, sv in v.items() if sk != "status"}
                                            for k, v in steps.items()}
        total_return = steps.get("monitor", {}).get("total_return", 0)
        exp.feedback = ExperimentFeedback(
            decision=total_return > 0,
            reason=f"Pipeline completed. Return: {total_return}",
            metrics={"total_return_pct": float(total_return) if total_return else 0.0},
        )
        trace.record(exp)
    except Exception as _e:
        logger.warning(f"Trace recording failed (non-blocking): {_e}")
    return signals




if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    capital_arg = float(sys.argv[2]) if len(sys.argv) > 2 else None
    result = run(date_arg, capital_arg)
    import json
    print(json.dumps(result, indent=2, default=str))
