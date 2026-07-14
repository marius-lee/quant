
"""EvaluationRepo — evaluation_runs, factor_snapshot operations."""

from __future__ import annotations

import json
import logging
from typing import Optional

from quant.data.repos._base import DatabaseManager, query_all, query_row, query_scalar

logger = logging.getLogger(__name__)


class EvaluationRepo:
    """Operations for evaluation results and factor snapshots."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None,
                 db_path: str = "data/market.db"):
        self.db = db_manager or DatabaseManager.get_instance()
        self.db_path = db_path

    def _conn(self):
        return self.db.get_connection(self.db_path)

    def save_evaluation(self, phase: str, data_json: str,
                        n_factors: int, n_passed: int) -> int:
        conn = self._conn()
        conn.execute(
            "INSERT INTO evaluation_runs (run_ts, phase, data_json, n_factors, n_passed) "
            "VALUES (datetime('now'), ?, ?, ?, ?)",
            (phase, data_json, n_factors, n_passed))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_latest(self, phase: str | None = None) -> dict | None:
        conn = self._conn()
        if phase:
            row = query_row(conn,
                "SELECT * FROM evaluation_runs WHERE phase=? ORDER BY run_ts DESC LIMIT 1",
                (phase,))
        else:
            row = query_row(conn,
                "SELECT * FROM evaluation_runs ORDER BY run_ts DESC LIMIT 1")
        return dict(row) if row else None

    def get_by_phase(self, phase: str, limit: int = 10) -> list[dict]:
        conn = self._conn()
        rows = query_all(conn,
            "SELECT * FROM evaluation_runs WHERE phase=? ORDER BY run_ts DESC LIMIT ?",
            (phase, limit))
        return [dict(r) for r in rows]

    def count_factors(self) -> int:
        conn = self._conn()
        return query_scalar(conn, "SELECT COUNT(*) FROM factor_registry") or 0
