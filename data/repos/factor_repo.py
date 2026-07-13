
"""FactorRepo — factor_registry CRUD operations."""

from __future__ import annotations

import logging
from typing import Optional

from data.repos._base import DatabaseManager, query_all, query_row

logger = logging.getLogger(__name__)

VALID_STATUSES = frozenset({"registered", "candidate", "active", "monitoring", "using",
                             "retired", "rejected", "backtesting"})


class FactorRepo:
    """CRUD operations for factor_registry table."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None,
                 db_path: str = "data/market.db"):
        self.db = db_manager or DatabaseManager.get_instance()
        self.db_path = db_path

    def _conn(self):
        return self.db.get_connection(self.db_path)

    def get_factors_by_status(self, statuses: tuple[str, ...],
                              names: list[str]) -> list[dict]:
        """Return factors with given statuses, filtered by name list."""
        if not names:
            return []
        conn = self._conn()
        ph_status = ",".join("?" * len(statuses))
        ph_names = ",".join("?" * len(names))
        rows = query_all(conn,
            f"SELECT name, category, ic_mean, status, status_reason "
            f"FROM factor_registry "
            f"WHERE status IN ({ph_status}) AND name IN ({ph_names})",
            tuple(statuses) + tuple(names))
        return [dict(r) for r in rows]

    def get_factor_by_name(self, name: str) -> dict | None:
        conn = self._conn()
        row = query_row(conn,
            "SELECT name, category, ic_mean, status, status_reason "
            "FROM factor_registry WHERE name=?",
            (name,))
        return dict(row) if row else None

    def update_status(self, name: str, status: str, reason: str = "") -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid factor status: {status}")
        conn = self._conn()
        conn.execute(
            "UPDATE factor_registry SET status=?, status_reason=? WHERE name=?",
            (status, reason, name))
        conn.commit()
        return conn.total_changes > 0

    def batch_set_status(self, names: list[str], status: str,
                         reason: str = "") -> int:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid factor status: {status}")
        if not names:
            return 0
        conn = self._conn()
        conn.executemany(
            "UPDATE factor_registry SET status=?, status_reason=? WHERE name=?",
            [(status, reason, n) for n in names])
        conn.commit()
        return conn.total_changes

    def status_distribution(self) -> dict[str, int]:
        conn = self._conn()
        dist = {}
        for r in query_all(conn, "SELECT status, COUNT(*) as cnt FROM factor_registry GROUP BY status"):
            dist[r["status"]] = r["cnt"]
        return dist

    def all_factor_names(self) -> list[str]:
        conn = self._conn()
        rows = query_all(conn, "SELECT name FROM factor_registry")
        return [r["name"] for r in rows]
