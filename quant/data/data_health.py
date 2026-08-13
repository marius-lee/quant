"""数据完整性审计 + 自动补拉修复 — v479 闭环核心.

审计规则 (对 registry 中每张表):
  freshness  — MAX(date) 滞后 > slo_days → fail (事件型 slo=None 跳过)
  gap_dates  — 最近 lookback 交易日内 daily 有而本表缺的日期 → fail
  coverage   — 最近 5 交易日每日行数 < min_rows_per_day → fail (事件型跳过)
  total_rows — COUNT(*) < min_total_rows → fail (累计底线)
  custom     — registry.custom_check(conn) → (ok, detail)

闭环 (晚间链 / 早间补拉链):
  audit_all → 写 data_audit → 失败表 repair (按模式重拉窗口/全量) → re-audit
  → 全过 = 修复 (repaired=1); 仍败 → 留 fail 供次日早间链重试 + 连续失败告警.

零 fallback: 不吞错 — 同步异常向上抛, 由调用方 (晚间链) 汇总为 partial/failed.
"""
import sqlite3
from datetime import datetime, date as _date, timedelta as _td

from quant.config.paths import MARKET_DB
from quant.utils.logger import get_logger
from quant.data.table_registry import TableSpec, REGISTRY

_log = get_logger("data.health")

AUDIT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS data_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,          -- 运行日
    table_name TEXT NOT NULL,
    rule       TEXT NOT NULL,          -- freshness|gap_dates|coverage|total_rows|custom
    status     TEXT NOT NULL,          -- ok|fail
    detail     TEXT,
    repaired   INTEGER DEFAULT 0,      -- 1 = 当晚/早间已自动修复
    checked_at TEXT NOT NULL,
    UNIQUE(date, table_name, rule)
)
"""


def ensure_audit_table(conn: sqlite3.Connection) -> None:
    conn.execute(AUDIT_TABLE_SQL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_date ON data_audit(date)")


def _recent_trading_days(conn: sqlite3.Connection, n: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily ORDER BY date DESC LIMIT ?", (n,)).fetchall()
    return [r[0] for r in rows]


def audit_table(conn: sqlite3.Connection, spec: TableSpec,
                today: str, lookback_days: int = 10) -> list[tuple[str, str, str]]:
    """单表完整性审计 → [(rule, status, detail)]. 不写库."""
    out: list[tuple[str, str, str]] = []
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (spec.table,)).fetchone()

    # 1) freshness — MAX(date) 滞后
    if spec.slo_days is not None:
        if not has_table:
            out.append(("freshness", "fail", f"表 {spec.table} 不存在"))
        else:
            mx = conn.execute(
                f"SELECT MAX({spec.date_col}) FROM {spec.table}").fetchone()[0]
            if mx is None:
                out.append(("freshness", "fail", "空表"))
            else:
                lag = (_date.fromisoformat(today) - _date.fromisoformat(str(mx)[:10])).days
                status = "ok" if lag <= spec.slo_days else "fail"
                out.append(("freshness", status, f"max={str(mx)[:10]} lag={lag}d slo={spec.slo_days}d"))
    elif not has_table:
        out.append(("freshness", "fail", f"表 {spec.table} 不存在"))

    # 2) gap_dates — 最近 lookback 交易日 daily 有而本表缺
    if has_table and spec.table != "daily" and spec.date_col == "date":
        trading = _recent_trading_days(conn, lookback_days)
        if trading:
            lo, hi = trading[-1], trading[0]
            have = {r[0] for r in conn.execute(
                f"SELECT DISTINCT {spec.date_col} FROM {spec.table} "
                f"WHERE {spec.date_col} BETWEEN ? AND ?", (lo, hi)).fetchall()}
            missing = [d for d in trading if d not in have]
            status = "ok" if not missing else "fail"
            out.append(("gap_dates", status, f"近{len(trading)}交易日缺 {len(missing)}: {missing[-5:]}"))
        else:
            out.append(("gap_dates", "ok", "daily 无数据, 跳过"))

    # 3) coverage — 最近 5 交易日每日行数
    if spec.min_rows_per_day is not None and has_table:
        recent = _recent_trading_days(conn, 5)
        if recent:
            lo = recent[-1]
            rows = dict(conn.execute(
                f"SELECT {spec.date_col}, COUNT(*) FROM {spec.table} "
                f"WHERE {spec.date_col} >= ? GROUP BY {spec.date_col}", (lo,)).fetchall())
            bad = {d: rows.get(d, 0) for d in recent if rows.get(d, 0) < spec.min_rows_per_day}
            status = "ok" if not bad else "fail"
            detail = "; ".join(f"{d}={v}行" for d, v in list(bad.items())[:3])
            if not bad:
                detail = f"近{len(recent)}交易日均 ≥{spec.min_rows_per_day}行"
            out.append(("coverage", status, detail))

    # 4) total_rows — 累计底线
    if spec.min_total_rows is not None and has_table:
        n = conn.execute(f"SELECT COUNT(*) FROM {spec.table}").fetchone()[0]
        status = "ok" if n >= spec.min_total_rows else "fail"
        out.append(("total_rows", status, f"{n} 行 (底线 {spec.min_total_rows})"))

    # 5) custom
    if spec.custom_check is not None and has_table:
        ok, detail = spec.custom_check(conn)
        out.append(("custom", "ok" if ok else "fail", detail))

    return out


def _write_audit(conn: sqlite3.Connection, today: str, spec: TableSpec,
                 results: list[tuple[str, str, str]]) -> None:
    for rule, status, detail in results:
        conn.execute(
            """INSERT OR REPLACE INTO data_audit
               (date, table_name, rule, status, detail, repaired, checked_at)
               VALUES (?,?,?,?,?,?,?)""",
            (today, spec.table, rule, status, detail, 0,
             datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()


def audit_all(today: str = None, db_path: str = None) -> dict[str, dict[str, str]]:
    """全表审计 + 写 data_audit. 返回 {table: {rule: status}}."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(db_path or MARKET_DB, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        ensure_audit_table(conn)
        result: dict[str, dict[str, str]] = {}
        for name, spec in REGISTRY.items():
            results = audit_table(conn, spec, today)
            _write_audit(conn, today, spec, results)
            result[name] = {r[0]: r[1] for r in results}
        return result
    finally:
        conn.close()


def failed_tables_on(day: str, db_path: str = None) -> list[str]:
    """指定日期审计中 status='fail' 的表 (早间补拉链读取昨日)."""
    conn = sqlite3.connect(db_path or MARKET_DB, timeout=30)
    try:
        ensure_audit_table(conn)
        rows = conn.execute(
            "SELECT DISTINCT table_name FROM data_audit WHERE date=? AND status='fail'",
            (day,)).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def last_ok_check(table: str, db_path: str = None) -> str | None:
    """该表最近一次全规则 ok 的 checked_at (weekly_full 兜底判定)."""
    conn = sqlite3.connect(db_path or MARKET_DB, timeout=30)
    try:
        ensure_audit_table(conn)
        row = conn.execute(
            "SELECT MAX(checked_at) FROM data_audit WHERE table_name=? AND status='ok'",
            (table,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def consecutive_failures(table: str, days: int = 5, db_path: str = None) -> int:
    """该表近 N 天内出现 fail 的独立日期数 (告警升级: ≥3 → ERROR)."""
    conn = sqlite3.connect(db_path or MARKET_DB, timeout=30)
    try:
        ensure_audit_table(conn)
        since = (datetime.now() - _td(days=days)).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM data_audit "
            "WHERE table_name=? AND status='fail' AND date >= ?",
            (table, since)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def repair_table(conn: sqlite3.Connection, spec: TableSpec, today: str) -> int:
    """按模式补拉单表, 返回写入行数 (同步异常向上抛, 不吞)."""
    if spec.mode == "rollback":
        from datetime import date as _d, timedelta as _td2
        end = today
        start = (_d.fromisoformat(today) - _td2(days=spec.window_days)).strftime("%Y-%m-%d")
        return spec.sync_main(start, end)
    if spec.mode == "weekly_full":
        return spec.sync_main()
    raise ValueError(f"mode={spec.mode} 无自动补拉方式 (primary/none 由主流程负责)")


def repair_and_reaudit(today: str, tables: list[str],
                       db_path: str = None) -> tuple[list[str], list[str]]:
    """对失败表补拉 + 重审计. 返回 (已修复表, 仍失败表).

    修复语义: 补拉后该表所有规则全 ok → data_audit 标记 repaired=1;
    仍有 fail → 保留 fail 行 (次日早间链再试).
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(db_path or MARKET_DB, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    repaired: list[str] = []
    still: list[str] = []
    try:
        ensure_audit_table(conn)
        for name in tables:
            spec = REGISTRY.get(name)
            if spec is None or spec.sync_main is None:
                _log.warning(f"repair: {name} 无同步函数, 跳过")
                still.append(name)
                continue
            try:
                n = repair_table(conn, spec, today)
                _log.info(f"repair {name}: +{n} rows")
            except Exception as e:
                _log.error(f"repair {name} failed: {e}")
                # 补拉失败也留痕 (rule='repair') — 次日早间链据此重试
                conn.execute(
                    """INSERT OR REPLACE INTO data_audit
                       (date, table_name, rule, status, detail, repaired, checked_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (today, name, "repair", "fail", f"补拉异常: {str(e)[:200]}", 0,
                     datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
                conn.commit()
                still.append(name)
                continue
            results = audit_table(conn, spec, today)
            _write_audit(conn, today, spec, results)
            if all(status == "ok" for _, status, _ in results):
                conn.execute(
                    "UPDATE data_audit SET repaired=1 WHERE date=? AND table_name=?",
                    (today, name))
                conn.commit()
                repaired.append(name)
            else:
                still.append(name)
        return repaired, still
    finally:
        conn.close()
