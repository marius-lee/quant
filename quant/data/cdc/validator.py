"""数据校验器 — CDC 同步后的质量保证.

设计:
  - 行级校验: 关键列非空、类型、范围
  - 表级校验: 行数、日期连续性、主键唯一性
  - 跨表校验: 引用完整性 (如 daily.symbol 必在 stocks 中)
  - 统计校验: 分布一致性 (均值、标准差、分位数)
  - 审计: 校验失败自动记录 + 告警
"""

from __future__ import annotations
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable
from quant.config.paths import MARKET_DB
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

logger = get_logger("data.cdc.validator")


@dataclass
class ValidationRule:
    """单条校验规则."""
    name: str
    table: str
    check_fn: Callable[[sqlite3.Connection], tuple[bool, str]]  # 返回 (passed, detail)
    severity: str = "error"  # error | warning | info
    enabled: bool = True


@dataclass
class ValidationResult:
    """校验结果."""
    rule_name: str
    table: str
    passed: bool
    detail: str
    severity: str
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


class DataValidator:
    """数据校验器."""

    def __init__(self, db_path: str = MARKET_DB):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._rules: List[ValidationRule] = []
        self._register_default_rules()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _register_default_rules(self):
        """注册默认校验规则."""
        # 1. daily 表: 主键唯一
        self.add_rule(ValidationRule(
            name="daily_pk_unique",
            table="daily",
            check_fn=lambda c: self._check_pk_unique(c, "daily", ["symbol", "date"]),
            severity="error",
        ))

        # 2. daily 表: 关键列非空
        self.add_rule(ValidationRule(
            name="daily_not_null",
            table="daily",
            check_fn=lambda c: self._check_not_null(c, "daily", ["symbol", "date", "open", "high", "low", "close"]),
            severity="error",
        ))

        # 3. daily 表: 价格合理性 (high >= low, high >= open, high >= close)
        self.add_rule(ValidationRule(
            name="daily_price_sanity",
            table="daily",
            check_fn=lambda c: self._check_price_sanity(c, "daily"),
            severity="warning",
        ))

        # 4. daily 表: 成交量非负
        self.add_rule(ValidationRule(
            name="daily_volume_nonneg",
            table="daily",
            check_fn=lambda c: self._check_nonneg(c, "daily", ["volume", "amount"]),
            severity="error",
        ))

        # 5. stocks 表: symbol 唯一
        self.add_rule(ValidationRule(
            name="stocks_pk_unique",
            table="stocks",
            check_fn=lambda c: self._check_pk_unique(c, "stocks", ["symbol"]),
            severity="error",
        ))

        # 6. 跨表: daily.symbol 必在 stocks 中
        self.add_rule(ValidationRule(
            name="daily_symbol_in_stocks",
            table="daily",
            check_fn=lambda c: self._check_fk(c, "daily", "symbol", "stocks", "symbol"),
            severity="warning",
        ))

        # 7. adj_factor: 因子为正
        self.add_rule(ValidationRule(
            name="adj_factor_positive",
            table="adj_factor",
            check_fn=lambda c: self._check_positive(c, "adj_factor", ["factor"]),
            severity="error",
        ))

        # 8. 每日行数不低于阈值
        self.add_rule(ValidationRule(
            name="daily_min_rows_per_day",
            table="daily",
            check_fn=lambda c: self._check_min_rows_per_day(c, "daily", 4000),
            severity="warning",
        ))

    def add_rule(self, rule: ValidationRule):
        self._rules.append(rule)

    def remove_rule(self, name: str):
        self._rules = [r for r in self._rules if r.name != name]

    def run_validations(self, tables: Optional[List[str]] = None) -> List[ValidationResult]:
        """运行校验."""
        conn = self._get_conn()
        results = []

        for rule in self._rules:
            if not rule.enabled:
                continue
            if tables and rule.table not in tables:
                continue

            try:
                passed, detail = rule.check_fn(conn)
                results.append(ValidationResult(
                    rule_name=rule.name,
                    table=rule.table,
                    passed=passed,
                    detail=detail,
                    severity=rule.severity,
                ))
                if not passed:
                    logger.warning(f"Validation FAILED: {rule.name} on {rule.table}: {detail}")
                else:
                    logger.debug(f"Validation PASSED: {rule.name} on {rule.table}")
            except Exception as e:
                logger.error(f"Validation ERROR: {rule.name} on {rule.table}: {e}")
                results.append(ValidationResult(
                    rule_name=rule.name,
                    table=rule.table,
                    passed=False,
                    detail=f"Check failed: {e}",
                    severity="error",
                ))

        return results

    # ══════════════════════════════════════════════════════════════════
    # 内置校验函数
    # ══════════════════════════════════════════════════════════════════

    def _check_pk_unique(self, conn: sqlite3.Connection, table: str, pk_cols: List[str]) -> tuple[bool, str]:
        pk_str = ", ".join(pk_cols)
        row = conn.execute(
            f"SELECT COUNT(*) as dup FROM (SELECT {pk_str}, COUNT(*) as cnt FROM {table} GROUP BY {pk_str} HAVING cnt > 1)"
        ).fetchone()
        dup = row["dup"] if row else 0
        return (dup == 0, f"Primary key duplicates: {dup}")

    def _check_not_null(self, conn: sqlite3.Connection, table: str, cols: List[str]) -> tuple[bool, str]:
        for col in cols:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table} WHERE {col} IS NULL").fetchone()
            if row and row["cnt"] > 0:
                return (False, f"Column {col} has {row['cnt']} NULL values")
        return (True, "All columns non-null")

    def _check_price_sanity(self, conn: sqlite3.Connection, table: str) -> tuple[bool, str]:
        row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM {table} WHERE high < low OR high < open OR high < close"
        ).fetchone()
        bad = row["cnt"] if row else 0
        return (bad == 0, f"Price sanity violations: {bad}")

    def _check_nonneg(self, conn: sqlite3.Connection, table: str, cols: List[str]) -> tuple[bool, str]:
        for col in cols:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table} WHERE {col} < 0").fetchone()
            if row and row["cnt"] > 0:
                return (False, f"Column {col} has {row['cnt']} negative values")
        return (True, "All columns non-negative")

    def _check_positive(self, conn: sqlite3.Connection, table: str, cols: List[str]) -> tuple[bool, str]:
        for col in cols:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table} WHERE {col} <= 0").fetchone()
            if row and row["cnt"] > 0:
                return (False, f"Column {col} has {row['cnt']} non-positive values")
        return (True, "All columns positive")

    def _check_fk(self, conn: sqlite3.Connection, child_table: str, child_col: str,
                  parent_table: str, parent_col: str) -> tuple[bool, str]:
        row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM {child_table} c LEFT JOIN {parent_table} p "
            f"ON c.{child_col} = p.{parent_col} WHERE p.{parent_col} IS NULL"
        ).fetchone()
        missing = row["cnt"] if row else 0
        return (missing == 0, f"FK violations: {missing} {child_table}.{child_col} not in {parent_table}")

    def _check_min_rows_per_day(self, conn: sqlite3.Connection, table: str, min_rows: int) -> tuple[bool, str]:
        row = conn.execute(
            f"SELECT date, COUNT(*) as cnt FROM {table} GROUP BY date HAVING cnt < ? ORDER BY cnt LIMIT 1",
            (min_rows,)
        ).fetchone()
        if row:
            return (False, f"Date {row['date']} has only {row['cnt']} rows (< {min_rows})")
        return (True, f"All days have >= {min_rows} rows")

    def get_failed_results(self, results: List[ValidationResult]) -> List[ValidationResult]:
        """获取失败的校验结果."""
        return [r for r in results if not r.passed]

    def has_errors(self, results: List[ValidationResult]) -> bool:
        """是否有 error 级别失败."""
        return any(not r.passed and r.severity == "error" for r in results)

    def has_warnings(self, results: List[ValidationResult]) -> bool:
        """是否有 warning 级别失败."""
        return any(not r.passed and r.severity == "warning" for r in results)


# 全局实例
_validator: Optional[DataValidator] = None


def get_validator() -> DataValidator:
    global _validator
    if _validator is None:
        _validator = DataValidator()
    return _validator