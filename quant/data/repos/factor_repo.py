"""FactorRepo — factor_registry CRUD operations + factor_ic_daily storage.

scope 列语义:
  'live'     — nightly attribution Step A 写入, active+monitoring 因子 (实盘归因)
  'backtest' — compute_backtest_ic() 写入, backtesting 池因子 (回测 IC 计算)
"""

from __future__ import annotations

import json
import logging
from quant.config.paths import MARKET_DB
from quant.data.repos._base import DatabaseManager, query_all, query_row, query_scalar

logger = logging.getLogger(__name__)


# ── 列名常量 (DDL 与查询共引, 改一处全局生效) ──
# factor_registry
FR_NAME        = "name"
FR_CATEGORY    = "category"
FR_STATUS      = "status"
FR_STATUS_REASON = "status_reason"
FR_IC_MEAN     = "ic_mean"
FR_IC_IR       = "ic_ir"
FR_HALF_LIFE   = "half_life"
FR_N_SIGNALS   = "n_signals"
FR_CONFIG      = "config"
FR_COMPUTE_FN  = "compute_fn"
FR_REGISTERED_AT = "registered_at"
FR_UPDATED_AT  = "updated_at"

# factor_ic_daily
FID_DATE         = "date"
FID_FACTOR_NAME  = "factor_name"
FID_IC_VALUE     = "ic_value"
FID_N_STOCKS     = "n_stocks"
FID_IS_IR        = "is_ir"
FID_OOS_IR       = "oos_ir"
FID_SCOPE        = "scope"
FID_CREATED_AT   = "created_at"

VALID_STATUSES = frozenset({"evaluating", "active", "probation", "archived"})
# ADR-040: 状态机简化 (方案 B)
#   evaluating: 新因子待评估 (原 candidate)
#   active:     通过评估, 实盘信号完整权重
#   probation:  IC 衰减观察期, 实盘信号衰减权重 (原 monitoring)
#   archived:   归档 (原 retired+rejected 合并, 用 status_reason 区分原因)
DEFAULT_SCOPE = "live"


class FactorRepo:
    """CRUD operations for factor_registry + factor_ic_daily tables."""

    def __init__(self, db_path: str = MARKET_DB):
        self.db_path = db_path

    def _conn(self):
        """新开 market.db 连接。调用方负责 commit + close。"""
        return DatabaseManager.get_connection(self.db_path)

    def _execute(self, sql: str, params: tuple = ()):
        """执行写操作，自动 commit + close。返回 cursor。"""
        conn = self._conn()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur
        finally:
            conn.close()

    def _query(self, sql: str, params: tuple = ()):
        """执行只读查询，自动 close。返回 fetchall。"""
        conn = self._conn()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _query_one(self, sql: str, params: tuple = ()):
        """执行只读查询，返回单行。"""
        conn = self._conn()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    # ── factor_registry queries ──

    def get_factors_by_status(self, statuses: tuple[str, ...],
                              names: list[str]) -> list[dict]:
        """Return factors with given statuses, filtered by name list."""
        if not names:
            return []
        ph_status = ",".join("?" * len(statuses))
        ph_names = ",".join("?" * len(names))
        rows = self._query(
            f"SELECT {FR_NAME}, {FR_CATEGORY}, {FR_IC_MEAN}, {FR_STATUS}, {FR_STATUS_REASON} "
            f"FROM factor_registry "
            f"WHERE {FR_STATUS} IN ({ph_status}) AND {FR_NAME} IN ({ph_names})",
            tuple(statuses) + tuple(names))
        return [dict(r) for r in rows]

    def get_all_by_status(self, statuses: tuple[str, ...]) -> list[dict]:
        """Return all factors with given statuses (no name filter)."""
        ph = ",".join("?" * len(statuses))
        rows = self._query(
            f"SELECT {FR_NAME}, {FR_CATEGORY}, {FR_IC_MEAN}, {FR_STATUS}, {FR_STATUS_REASON}, {FR_UPDATED_AT}, retry_count "
            f"FROM factor_registry WHERE {FR_STATUS} IN ({ph})",
            tuple(statuses))
        return [dict(r) for r in rows]

    def get_factor_by_name(self, name: str) -> dict | None:
        row = self._query_one(
            f"SELECT {FR_NAME}, {FR_CATEGORY}, {FR_IC_MEAN}, {FR_STATUS}, {FR_STATUS_REASON}, retry_count "
            f"FROM factor_registry WHERE {FR_NAME}=?",
            (name,))
        return dict(row) if row else None

    def update_status(self, name: str, status: str, reason: str = "",
                      retry_count: int | None = None) -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid factor status: {status}")
        if retry_count is not None:
            cur = self._execute(
                f"UPDATE factor_registry SET {FR_STATUS}=?, {FR_STATUS_REASON}=?, "
                f"retry_count=?, {FR_UPDATED_AT}=datetime('now','localtime') "
                f"WHERE {FR_NAME}=?",
                (status, reason, retry_count, name))
        else:
            cur = self._execute(
                f"UPDATE factor_registry SET {FR_STATUS}=?, {FR_STATUS_REASON}=?, "
                f"{FR_UPDATED_AT}=datetime('now','localtime') WHERE {FR_NAME}=?",
                (status, reason, name))
        return cur.rowcount > 0

    def batch_set_status(self, names: list[str], status: str,
                         reason: str = "") -> int:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid factor status: {status}")
        if not names:
            return 0
        conn = self._conn()
        try:
            conn.executemany(
                f"UPDATE factor_registry SET {FR_STATUS}=?, {FR_STATUS_REASON}=?, {FR_UPDATED_AT}=datetime('now','localtime') WHERE {FR_NAME}=?",
                [(status, reason, n) for n in names])
            conn.commit()
            return conn.total_changes
        finally:
            conn.close()

    def get_factors_with_ic(self, statuses: tuple[str, ...]) -> list[dict]:
        """Return factors with IC data for given statuses."""
        ph = ",".join("?" * len(statuses))
        rows = self._query(
            f"SELECT {FR_NAME}, {FR_IC_MEAN}, {FR_IC_IR}, {FR_STATUS} FROM factor_registry "
            f"WHERE {FR_STATUS} IN ({ph}) AND {FR_IC_MEAN} IS NOT NULL",
            tuple(statuses))
        return [dict(r) for r in rows]

    def get_all_factors(self) -> list[dict]:
        """Return all factors with their metadata."""
        rows = self._query(
            f"SELECT {FR_NAME}, {FR_CATEGORY}, {FR_STATUS}, {FR_STATUS_REASON}, {FR_IC_MEAN}, {FR_IC_IR} FROM factor_registry")
        return [dict(r) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        return self.status_distribution()

    def status_distribution(self) -> dict[str, int]:
        dist = {}
        for r in self._query(f"SELECT {FR_STATUS}, COUNT(*) as cnt FROM factor_registry GROUP BY {FR_STATUS}"):
            dist[r["status"]] = r["cnt"]
        return dist

    def count_with_ic(self) -> int:
        row = self._query_one(f"SELECT COUNT(*) FROM factor_registry WHERE {FR_IC_MEAN} IS NOT NULL")
        return row[0] if row else 0

    def count_total(self) -> int:
        row = self._query_one(f"SELECT COUNT(*) FROM factor_registry")
        return row[0] if row else 0

    def insert_or_update(self, name: str, category: str, status: str,
                         status_reason: str = "", ic_mean: float = None,
                         ic_ir: float = None, compute_fn: str = None,
                         source: str = None):
        conn = self._conn()
        try:
            existing = conn.execute(f"SELECT 1 FROM factor_registry WHERE {FR_NAME}=?", (name,)).fetchone()
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
                if source:
                    parts.append("academic_source=?")
                    params.append(source)
                params.append(name)
                conn.execute(f"UPDATE factor_registry SET {', '.join(parts)} WHERE name=?", params)
            else:
                conn.execute(
                    f"INSERT INTO factor_registry ({FR_NAME}, {FR_CATEGORY}, {FR_COMPUTE_FN}, {FR_STATUS}, {FR_STATUS_REASON}, {FR_IC_MEAN}, {FR_IC_IR}, academic_source, {FR_UPDATED_AT}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
                    (name, category, compute_fn or name, status, status_reason, ic_mean, ic_ir, source))
            conn.commit()
        finally:
            conn.close()

    def all_factor_names(self) -> list[str]:
        rows = self._query(f"SELECT {FR_NAME} FROM factor_registry")
        return [r["name"] for r in rows]

    def get_factor_updated_at(self, name: str) -> str | None:
        """Get updated_at timestamp for a factor (used for monitoring buffer check)."""
        row = self._query_one(f"SELECT {FR_UPDATED_AT} FROM factor_registry WHERE {FR_NAME}=?", (name,))
        return row[0] if row else None

    # ── factor_ic_daily — 每日因子 IC 记录 (含 scope 隔离) ──

    def ensure_ic_daily_table(self) -> None:
        """幂等建表 factor_ic_daily (含 scope 列)."""
        conn = self._conn()
        try:
            existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(factor_ic_daily)").fetchall()} if self._table_exists(conn, "factor_ic_daily") else set()
            if existing_cols and "scope" not in existing_cols:
                conn.executescript("""
                    DROP TABLE IF EXISTS factor_ic_daily_new;
                    CREATE TABLE factor_ic_daily_new (
                        date TEXT NOT NULL, factor_name TEXT NOT NULL, ic_value REAL,
                        n_stocks INTEGER, is_ir REAL, oos_ir REAL,
                        scope TEXT NOT NULL DEFAULT 'live',
                        created_at TEXT DEFAULT (datetime('now','localtime')),
                        PRIMARY KEY (date, factor_name, scope)
                    );
                """)
                conn.execute("INSERT OR IGNORE INTO factor_ic_daily_new SELECT date, factor_name, ic_value, n_stocks, is_ir, oos_ir, 'live', created_at FROM factor_ic_daily")
                conn.execute("DROP TABLE factor_ic_daily")
                conn.execute("ALTER TABLE factor_ic_daily_new RENAME TO factor_ic_daily")
            else:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS factor_ic_daily (
                        date TEXT NOT NULL, factor_name TEXT NOT NULL, ic_value REAL,
                        n_stocks INTEGER, is_ir REAL, oos_ir REAL,
                        scope TEXT NOT NULL DEFAULT 'live',
                        created_at TEXT DEFAULT (datetime('now','localtime')),
                        PRIMARY KEY (date, factor_name, scope)
                    );
                """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fic_date ON factor_ic_daily(date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fic_factor ON factor_ic_daily(factor_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fic_scope ON factor_ic_daily(scope)")
            conn.commit()
        finally:
            conn.close()

    def _table_exists(self, conn, table_name: str) -> bool:
        return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()[0] > 0

    def insert_ic_daily(self, date: str, factor_name: str,
                        ic_value: float, n_stocks: int,
                        is_ir: float = None, oos_ir: float = None,
                        scope: str = DEFAULT_SCOPE) -> None:
        """写入每日因子 IC 记录。INSERT OR REPLACE 语义。

        scope: 'live' (实盘归因) 或 'backtest' (回测 IC 计算).
        """
        self._execute(
            f"INSERT OR REPLACE INTO factor_ic_daily ({FID_DATE}, {FID_FACTOR_NAME}, {FID_IC_VALUE}, {FID_N_STOCKS}, {FID_IS_IR}, {FID_OOS_IR}, {FID_SCOPE}, {FID_CREATED_AT}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
            (date, factor_name, ic_value, n_stocks, is_ir, oos_ir, scope))

    def get_ic_rolling(self, factor_name: str, n_days: int = 20,
                       scope: str = DEFAULT_SCOPE) -> list[dict]:
        """读取某因子最近 n_days 的 IC 记录 (指定 scope)."""
        rows = self._query(
            f"SELECT {FID_DATE}, {FID_IC_VALUE}, {FID_IS_IR}, {FID_OOS_IR} FROM factor_ic_daily "
            f"WHERE {FID_FACTOR_NAME}=? AND {FID_SCOPE}=? ORDER BY {FID_DATE} DESC LIMIT ?",
            (factor_name, scope, n_days))
        return [{"date": r[0], "ic_value": r[1], "is_ir": r[2], "oos_ir": r[3]} for r in reversed(rows)]

    def get_ic_rolling_all(self, factor_names: list[str], n_days: int = 20,
                           scope: str = DEFAULT_SCOPE) -> dict:
        """批量读取所有因子最近 n_days IC 序列 (指定 scope). {name: [ic_values]}"""
        if not factor_names:
            return {}
        ph = ",".join("?" * len(factor_names))
        rows = self._query(
            f"SELECT {FID_FACTOR_NAME}, {FID_IC_VALUE} FROM factor_ic_daily "
            f"WHERE {FID_FACTOR_NAME} IN ({ph}) AND {FID_SCOPE}=? "
            f"AND {FID_DATE} IN (SELECT DISTINCT {FID_DATE} FROM factor_ic_daily WHERE {FID_SCOPE}=? ORDER BY {FID_DATE} DESC LIMIT ?) "
            f"ORDER BY {FID_DATE}",
            tuple(factor_names) + (scope, scope, n_days))
        result: dict[str, list[float]] = {n: [] for n in factor_names}
        for r in rows:
            result[r[0]].append(r[1])
        return result

    def sync_ic_mean_to_registry(self, name: str, ic_mean: float, n_days: int = 60,
                                 scope: str = DEFAULT_SCOPE) -> None:
        """将最近 n_days 滚动均值写回 factor_registry.ic_mean (指定 scope)."""
        recent = self.get_ic_rolling(name, n_days, scope=scope)
        if recent:
            vals = [r["ic_value"] for r in recent if r["ic_value"] is not None]
            if vals:
                ic_mean = sum(vals) / len(vals)
        self._execute(
            f"UPDATE factor_registry SET {FR_IC_MEAN}=?, {FR_UPDATED_AT}=datetime('now','localtime') WHERE {FR_NAME}=?",
            (ic_mean, name))

    def sync_all_ic_means(self, factor_names: list[str], n_days: int = 60,
                          scope: str = DEFAULT_SCOPE) -> None:
        """批量同步所有因子的 ic_mean 到 factor_registry (指定 scope)."""
        ic_map = self.get_ic_rolling_all(factor_names, n_days, scope=scope)
        for name in factor_names:
            vals = ic_map.get(name, [])
            if vals:
                mu = sum(vals) / len(vals)
                self.sync_ic_mean_to_registry(name, mu, n_days, scope=scope)
