"""Pipeline run state store — replaces /tmp JSON files with DB-backed persistence.

DB table: evaluation_runs(id, run_ts, phase, data_json, n_factors, n_passed)

ADR 028: all evaluation pipeline stages write results to evaluation_runs table
instead of ad-hoc /tmp JSON files. Keeps full history for audit and debugging.
"""
import json
import sqlite3
import os
from config.constants import _require_cfg
from datetime import datetime

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")


def save_phase(phase: str, data: dict) -> int:
    """Save a phase result to evaluation_runs. Returns row id."""
    from data.repos import EvaluationRepo
    data_json = json.dumps(data, ensure_ascii=False, default=str)
    n_factors = data.get("n_factors") or len(data.get("factors", []))
    n_passed = len(data.get("passed", []))
    repo = EvaluationRepo()
    return repo.save_evaluation(phase, data_json, n_factors, n_passed)


def load_latest(phase: str) -> dict | None:
    """Load the most recent result for a given phase. Returns None if no rows."""
    from data.repos import EvaluationRepo
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
