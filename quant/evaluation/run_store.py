"""Pipeline run state store — replaces /tmp JSON files with DB-backed persistence.

DB table: evaluation_runs(id, run_ts, phase, data_json, n_factors, n_passed)

ADR 028: all evaluation pipeline stages write results to evaluation_runs table
instead of ad-hoc /tmp JSON files. Keeps full history for audit and debugging.
"""
import json
import sqlite3
import os
from quant.config.constants import _require_cfg
from datetime import datetime

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")


def save_phase(phase: str, data: dict) -> int:
    """Save a phase result to evaluation_runs. Returns row id."""
    from quant.data.repos import EvaluationRepo
    data_json = json.dumps(data, ensure_ascii=False, default=str)
    n_factors = data.get("n_factors") or len(data.get("factors", []))
    n_passed = len(data.get("passed", []))
    repo = EvaluationRepo()
    row_id = repo.save_evaluation(phase, data_json, n_factors, n_passed)

    # ── Record Experiment in Trace (non-blocking) ──
    import logging as _log_eval
    try:
        from quant.core.trace import get_trace, make_experiment, Hypothesis, ExperimentFeedback
        trace = get_trace()
        parent = trace.last_of(f"eval_{phase}")
        exp = make_experiment(
            action=f"eval_{phase}",
            hypothesis=Hypothesis(
                hypothesis=f"Phase {phase}: {n_factors} factors evaluated",
                reason=f"Standard evaluation pipeline phase {phase}",
                source=f"evaluation/save_phase({phase})",
            ),
            parent_id=parent.experiment_id if parent else None,
        )
        exp.sub_results = data
        exp.feedback = ExperimentFeedback(
            decision=n_passed > 0,
            reason=f"Phase {phase}: {n_passed}/{n_factors} factors passed",
            metrics={"n_factors": n_factors, "n_passed": n_passed},
        )
        trace.record(exp)
    except Exception as _e:
        _log_eval.warning(f"Trace recording failed (non-blocking): {_e}")

    return row_id


def load_latest(phase: str) -> dict | None:
    """Load the most recent result for a given phase. Returns None if no rows."""
    from quant.data.repos import EvaluationRepo
    repo = EvaluationRepo()
    row = repo.get_latest(phase)
    if row and row.get("data_json"):
        return json.loads(row["data_json"])
    return None


def list_runs(phase: str = None, limit: int = 10) -> list:
    """List recent runs, optionally filtered by phase."""
    conn = sqlite3.connect(_DB_PATH, timeout=_require_cfg("data.sqlite.timeout"))
    if phase:
        rows = conn.execute(
            """SELECT id, run_ts, phase, n_factors, n_passed
               FROM evaluation_runs WHERE phase=?
               ORDER BY run_ts DESC LIMIT ?""",
            (phase, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, run_ts, phase, n_factors, n_passed
               FROM evaluation_runs
               ORDER BY run_ts DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    conn.close()
    return [{"id": r[0], "run_ts": r[1], "phase": r[2],
             "n_factors": r[3], "n_passed": r[4]} for r in rows]
