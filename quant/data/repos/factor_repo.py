"""FactorRepo — factor_registry CRUD operations + factor_ic_daily storage."""

from __future__ import annotations

import json
import logging
from typing import Optional

from quant.data.repos._base import DatabaseManager, query_all, query_row

logger = logging.getLogger(__name__)

VALID_STATUSES = frozenset({"registered", "candidate", "active", "monitoring", "retired", "rejected"})


class FactorRepo:
    """CRUD operations for factor_registry + factor_ic_daily tables."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None,
                 db_path: str = "quant/data/market.db"):
        self.db = db_manager or DatabaseManager.get_instance()
        self.db_path = db_path

    def _conn(self):
        return self.db.get_connection(self.db_path)

    # ── factor_registry queries ──

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

    def get_all_by_status(self, statuses: tuple[str, ...]) -> list[dict]:
        """Return all factors with given statuses (no name filter)."""
        conn = self._conn()
        ph = ",".join("?" * len(statuses))
        rows = query_all(conn,
            f"SELECT name, category, ic_mean, status, status_reason, updated_at "
            f"FROM factor_registry WHERE status IN ({ph})",
            tuple(statuses))
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
            "UPDATE factor_registry SET status=?, status_reason=?, updated_at=datetime('now','localtime') WHERE name=?",
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
            "UPDATE factor_registry SET status=?, status_reason=?, updated_at=datetime('now','localtime') WHERE name=?",
            [(status, reason, n) for n in names])
        conn.commit()
        return conn.total_changes

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
        return self.status_distribution()

    def status_distribution(self) -> dict[str, int]:
        conn = self._conn()
        dist = {}
        for r in query_all(conn, "SELECT status, COUNT(*) as cnt FROM factor_registry GROUP BY status"):
            dist[r["status"]] = r["cnt"]
        return dist

    def count_with_ic(self) -> int:
        conn = self._conn()
        return query_scalar(conn,
            "SELECT COUNT(*) FROM factor_registry WHERE ic_mean IS NOT NULL") or 0

    def count_total(self) -> int:
        conn = self._conn()
        return query_scalar(conn, "SELECT COUNT(*) FROM factor_registry") or 0

    def insert_or_update(self, name: str, category: str, status: str,
                         status_reason: str = "", ic_mean: float = None,
                         ic_ir: float = None, compute_fn: str = None):
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

    def get_factor_updated_at(self, name: str) -> str | None:
        """Get updated_at timestamp for a factor (used for monitoring buffer check)."""
        conn = self._conn()
        row = conn.execute(
            "SELECT updated_at FROM factor_registry WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    # ── factor_ic_daily — 每日因子 IC 记录 (Level 1 数据源) ──

    def ensure_ic_daily_table(self) -> None:
        """幂等建表 factor_ic_daily."""
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS factor_ic_daily (
                date        TEXT NOT NULL,
                factor_name TEXT NOT NULL,
                ic_value    REAL,
                n_stocks    INTEGER,
                is_ir       REAL,
                oos_ir      REAL,
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (date, factor_name)
            );
            CREATE INDEX IF NOT EXISTS idx_fic_date ON factor_ic_daily(date);
            CREATE INDEX IF NOT EXISTS idx_fic_factor ON factor_ic_daily(factor_name);
        """)
        conn.commit()

    def insert_ic_daily(self, date: str, factor_name: str,
                        ic_value: float, n_stocks: int,
                        is_ir: float = None, oos_ir: float = None) -> None:
        """写入每日因子 IC 记录。INSERT OR REPLACE 语义。"""
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO factor_ic_daily (date, factor_name, ic_value, n_stocks, is_ir, oos_ir, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
            (date, factor_name, ic_value, n_stocks, is_ir, oos_ir))
        conn.commit()

    def get_ic_rolling(self, factor_name: str, n_days: int = 20) -> list[dict]:
        """读取某因子最近 n_days 的 IC 记录。"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT date, ic_value, is_ir, oos_ir FROM factor_ic_daily "
            "WHERE factor_name=? ORDER BY date DESC LIMIT ?",
            (factor_name, n_days)).fetchall()
        return [{"date": r[0], "ic_value": r[1], "is_ir": r[2], "oos_ir": r[3]} for r in reversed(rows)]

    def get_ic_rolling_all(self, factor_names: list[str], n_days: int = 20) -> dict:
        """批量读取所有因子最近 n_days IC 序列。{name: [ic_values]}"""
        if not factor_names:
            return {}
        conn = self._conn()
        ph = ",".join("?" * len(factor_names))
        rows = conn.execute(
            f"SELECT factor_name, ic_value FROM factor_ic_daily "
            f"WHERE factor_name IN ({ph}) "
            f"AND date IN (SELECT DISTINCT date FROM factor_ic_daily ORDER BY date DESC LIMIT ?) "
            f"ORDER BY date",
            tuple(factor_names) + (n_days,)).fetchall()
        result: dict[str, list[float]] = {n: [] for n in factor_names}
        for r in rows:
            result[r[0]].append(r[1])
        return result

    def sync_ic_mean_to_registry(self, name: str, ic_mean: float, n_days: int = 60) -> None:
        """将最近 n_days 滚动均值写回 factor_registry.ic_mean。"""
        recent = self.get_ic_rolling(name, n_days)
        if recent:
            vals = [r["ic_value"] for r in recent if r["ic_value"] is not None]
            if vals:
                ic_mean = sum(vals) / len(vals)
        conn = self._conn()
        conn.execute(
            "UPDATE factor_registry SET ic_mean=?, updated_at=datetime('now','localtime') WHERE name=?",
            (ic_mean, name))
        conn.commit()

    def sync_all_ic_means(self, factor_names: list[str], n_days: int = 60) -> None:
        """批量同步所有因子的 ic_mean 到 factor_registry。"""
        ic_map = self.get_ic_rolling_all(factor_names, n_days)
        for name in factor_names:
            vals = ic_map.get(name, [])
            if vals:
                mu = sum(vals) / len(vals)
                self.sync_ic_mean_to_registry(name, mu, n_days)
from quant.data.repos._base import DatabaseManager, query_all, query_row, query_scalar
