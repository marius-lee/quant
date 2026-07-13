
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


    def get_factors_with_ic(self, statuses: tuple[str, ...]) -> list[dict]:
        """Return factors with IC data for given statuses."""
        conn = self._conn()
        ph = ",".join("?" * len(statuses))
        rows = query_all(conn,
            f"SELECT name, ic_mean, ic_ir, status FROM factor_registry "
            f"WHERE status IN ({ph}) AND ic_mean IS NOT NULL",
            tuple(statuses))
        return [dict(r) for r in rows]

    def get_all_factors(self) -> list[dict]:
        """Return all factors with their metadata."""
        conn = self._conn()
        rows = query_all(conn,
            "SELECT name, category, status, status_reason, ic_mean, ic_ir FROM factor_registry")
        return [dict(r) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        """Return {status: count} and total with IC."""
        return self.status_distribution()

    def count_with_ic(self) -> int:
        """Return count of factors that have IC data."""
        conn = self._conn()
        return query_scalar(conn,
            "SELECT COUNT(*) FROM factor_registry WHERE ic_mean IS NOT NULL") or 0

    def count_total(self) -> int:
        """Return total factor count."""
        conn = self._conn()
        return query_scalar(conn, "SELECT COUNT(*) FROM factor_registry") or 0

    def insert_or_update(self, name: str, category: str, status: str,
                         status_reason: str = "", ic_mean: float = None,
                         ic_ir: float = None, compute_fn: str = None):
        """Insert or update a factor registry entry."""
        conn = self._conn()
        existing = query_row(conn, "SELECT 1 FROM factor_registry WHERE name=?", (name,))
        if existing:
            parts = ["updated_at=datetime('now','localtime')"]
            params = []
            if status:
                parts.append("status=?")
                params.append(status)
            if status_reason:
                parts.append("status_reason=?")
                params.append(status_reason)
            if ic_mean is not None:
                parts.append("ic_mean=?")
                params.append(ic_mean)
            if ic_ir is not None:
                parts.append("ic_ir=?")
                params.append(ic_ir)
            params.append(name)
            conn.execute(f"UPDATE factor_registry SET {', '.join(parts)} WHERE name=?", params)
        else:
            conn.execute(
                "INSERT INTO factor_registry (name, category, compute_fn, status, status_reason, ic_mean, ic_ir, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
                (name, category, compute_fn or name, status, status_reason, ic_mean, ic_ir))
        conn.commit()

    def all_factor_names(self) -> list[str]:
        conn = self._conn()
        rows = query_all(conn, "SELECT name FROM factor_registry")
        return [r["name"] for r in rows]
