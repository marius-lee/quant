
"""EvaluationRepo — evaluation_runs, factor_snapshot operations."""

from __future__ import annotations

import json
import logging
from quant.config.paths import MARKET_DB
from quant.data.repos._base import DatabaseManager, query_all, query_row, query_scalar

logger = logging.getLogger(__name__)


class EvaluationRepo:
    """Operations for evaluation results and factor snapshots."""

    def __init__(self, db_path: str = MARKET_DB):
        self.db_path = db_path

    def _conn(self):
        return DatabaseManager.get_connection(self.db_path)

    def _query(self, sql, params=()):
        conn = self._conn()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _query_one(self, sql, params=()):
        conn = self._conn()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def _execute(self, sql, params=()):
        conn = self._conn()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur
        finally:
            conn.close()

    def save_evaluation(self, phase: str, data_json: str,
                        n_factors: int, n_passed: int) -> int:
        conn = self._conn()
        try:
            conn.execute(
            "INSERT INTO evaluation_runs (run_ts, phase, data_json, n_factors, n_passed) "
            "VALUES (datetime('now','localtime'), ?, ?, ?, ?)",
            (phase, data_json, n_factors, n_passed))
            conn.commit()
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        finally:
            conn.close()

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
