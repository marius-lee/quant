"""任务执行日志 — DB 驱动的进程间通信。

取代之前从 logs/quant.log 用正则刮取 [SCHEDULER] 标记的反模式。
每个任务的 _run() 在开始时 INSERT running 行，结束时 UPDATE 状态。

表结构 (market.db):
  CREATE TABLE IF NOT EXISTS task_runs (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      task_name  TEXT    NOT NULL,   -- signals|execute|monitor|attribution|weekly_eval
      date       TEXT    NOT NULL,   -- 2026-07-15
      started_at TEXT    NOT NULL,   -- ISO timestamp
      finished_at TEXT,              -- NULL until complete
      status     TEXT    NOT NULL,   -- running|ok|failed
      error      TEXT,               -- 失败原因
      summary    TEXT                -- JSON: {"targets":5,"elapsed":2.3}
  );
  CREATE INDEX IF NOT EXISTS idx_task_runs_date ON task_runs(date, task_name);
"""

import sqlite3
import json
import os
from datetime import datetime

from quant.config.paths import MARKET_DB
from quant.config.constants import _require_cfg


def _conn():
    """打开 market.db 连接 (WAL 模式 + 忙等待)."""
    c = sqlite3.connect(MARKET_DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
    return c


def _ensure_table():
    """幂等建表 (含 pid 列, test-v281: PID 检测进程死活)."""
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_runs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_name  TEXT    NOT NULL,
                date       TEXT    NOT NULL,
                started_at TEXT    NOT NULL,
                finished_at TEXT,
                status     TEXT    NOT NULL,
                pid        INTEGER,
                error      TEXT,
                summary    TEXT
            )
        """)
        # 兼容旧表: 无 pid 列时添加
        cols = [r[1] for r in conn.execute("PRAGMA table_info(task_runs)").fetchall()]
        if "pid" not in cols:
            conn.execute("ALTER TABLE task_runs ADD COLUMN pid INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_date ON task_runs(date, task_name)")
        conn.commit()
    finally:
        conn.close()


# ── 模块加载时自动建表 ──
_ensure_table()


def _pid_alive(pid: int) -> bool:
    """v424: 检查 pid 对应进程是否存活 (POSIX kill(0) 探测).

    None (旧数据无 pid) → 视为存活, 不误杀 (走超时逻辑兜底).
    """
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start(task_name: str, date: str, dedup: bool = False, grace_seconds: int = 120) -> int | None:
    """任务启动时调用。返回 row id, 若已运行则返回 None 表示跳过。

    Args:
        task_name: 'signals' | 'execute' | 'monitor' | 'attribution' | 'weekly_eval'
        date: '2026-07-15'
        dedup: 如果 True，同任务同日期仅保留一行（DELETE 旧行 + INSERT 新行）。
               适用于高频重复任务（如 monitor 每30s一次），防止 task_runs 膨胀。
        grace_seconds: running 行的宽限期(秒)。在此时间内视为"仍在运行"，返回 None。
                       超时则标为 aborted 并新建行。默认 120s。
                       ⚠ 必须 ≥ 任务合法最长运行时间, 否则 cron+daemon 双调度
                       的第二触发会误 abort 活着的任务 (僵尸进程继续持锁 →
                       下游 database is locked)。各任务对齐 orchestrator._TIMEOUTS。
    """
    conn = _conn()
    try:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # 检查是否已有 running 行 (test-v204: 防止双 orchestractor 重复触发)
        existing = conn.execute(
            "SELECT id, started_at, pid FROM task_runs "
            "WHERE task_name=? AND date=? AND status='running' "
            "ORDER BY id DESC LIMIT 1",
            (task_name, date)
        ).fetchone()

        if existing:
            rid, started, pid = existing
            dt = datetime.fromisoformat(started)
            elapsed = (datetime.now() - dt).total_seconds()
            if pid and not _pid_alive(pid):
                # v424: 记录进程已死 (pkill/崩溃) → 立即标 aborted, 不等 grace
                # (原逻辑等 grace_seconds 超时才 auto-abort, 期间界面一直"运行中")
                conn.execute(
                    "UPDATE task_runs SET status='aborted', finished_at=?, "
                    "error='进程已死 (pid=' || ? || ') — auto-abort' WHERE id=?",
                    (now, str(pid), rid)
                )
            elif elapsed < grace_seconds:
                # 近期已有运行中任务 → 跳过, 不创建重复行
                conn.close()
                return None
            else:
                # 超时僵尸 → 标为 aborted, 然后继续创建新行
                conn.execute(
                    "UPDATE task_runs SET status='aborted', finished_at=?, "
                    "error='超时未完成 (auto-abort, ' || ? || 's)' WHERE id=?",
                    (now, int(elapsed), rid)
                )

        if dedup:
            # 每天每任务最多一行 (2026-07-22: monitor防膨胀)
            conn.execute("DELETE FROM task_runs WHERE task_name=? AND date=?", (task_name, date))
        cur = conn.execute(
            "INSERT INTO task_runs (task_name, date, started_at, status, pid) VALUES (?, ?, ?, 'running', ?)",
            (task_name, date, now, os.getpid()))
        conn.commit()
        return cur.lastrowid
    finally:
        if conn:
            conn.close()


def finish(task_name: str, date: str, status: str,
           error: str = None, summary: dict = None):
    """任务完成时调用。更新最近一条 matching running 行。
    
    Args:
        status: 'ok' | 'failed'
        error: 失败时的异常信息
        summary: 可选 dict, 如 {"targets": 5, "elapsed": 2.3}
    """
    conn = _conn()
    try:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        summary_json = json.dumps(summary, ensure_ascii=False) if summary else None
        # 先查找 running 行
        row = conn.execute(
            "SELECT id, status FROM task_runs"
            " WHERE task_name = ? AND date = ?"
            " ORDER BY id DESC LIMIT 1",
            (task_name, date)
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"task_log.finish({task_name}, {date}): no row found"
            )
        rid, cur_status = row
        if cur_status not in ('running', 'lunch'):
            # v368: 防御 — 已被 _check_timeouts 标为 aborted 时不抛异常
            # v555 (F5): 'lunch' 是 monitor 的午休 stage (占 status 列),
            # 午休崩溃时 finish(failed) 若跳过则失败不落库 → 重试预算恒 0
            # → 午休期间崩溃-重启死循环, 且行永卡 'lunch' 无法自愈
            import logging
            _finish_log = logging.getLogger("task_log")
            _finish_log.warning(
                f"task_log.finish({task_name}, {date}): status already '{cur_status}' "
                f"(expected 'running'), skip update — likely auto-aborted by timeout checker"
            )
            conn.close()
            return
        conn.execute(
            """UPDATE task_runs
               SET finished_at = ?, status = ?, error = ?, summary = ?
               WHERE id = ?""",
            (now, status, error, summary_json, row[0])
        )
        conn.commit()
    finally:
        conn.close()


def query_date(date: str) -> list[dict]:
    """查询指定日期的所有任务执行记录。"""
    conn = _conn()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM task_runs WHERE date = ? ORDER BY id DESC",
            (date,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def last_status(task_name: str, date: str) -> str | None:
    """v420: 读该任务当日最后一条状态 (ok/failed/aborted/running), 无记录 → None.

    用途: _weekly_loop 重启补跑门控 — 重试语义以 DB 为准:
        'ok'      → 当日已完成, 不重跑 (防止随重启重复执行整周评估)
        'failed'  → 当日失败, 允许重跑 (修复部署后仍需补救验证)
        'aborted' → 超时中断, 允许重跑
        None      → 当日未跑过, 正常触发
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT status FROM task_runs WHERE task_name=? AND date=? "
            "ORDER BY id DESC LIMIT 1",
            (task_name, date)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def any_ok(task_name: str, date: str) -> bool:
    """v427: 当日是否存在过 ok 行.

    与 last_status 的区别: last_status 取最新一条, 僵尸 aborted 行 (v424 手动
    清理产生) 会遮蔽更早的 ok 行 → 门控误判"未完成" → 整周评估重复跑.
    any_ok 只看"这一天成功过没有", 稳定性优先.
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM task_runs WHERE task_name=? AND date=? AND status='ok' "
            "LIMIT 1",
            (task_name, date)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════
# P1a: 任务装饰器 — 消除 start/try/finish 样板代码
# ═══════════════════════════════════════════════════════════

def task(name: str, grace_seconds: int = 120):
    """调度任务装饰器: 自动管理 task_runs 的 running→ok/failed 生命周期.

    使用:
        @task("signals", grace_seconds=1800)
        def _run(today: str):
            ...  # 业务逻辑, return 可选 dict 作为 summary

    等价于:
        rid = _tk_start(name, today, grace_seconds=...)
        if rid is None: return
        try:
            result = fn(today)
            _tk_finish(name, today, "ok", summary=result)
        except Exception as e:
            _tk_finish(name, today, "failed", error=str(e))
            raise
    """
    import functools
    from quant.utils.logger import get_logger as _gl

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(today: str):
            _log = _gl(f"scheduler.{name}")
            rid = start(name, today, grace_seconds=grace_seconds)
            if rid is None:
                _log.info(f"[{today}] {name} already running, skip duplicate trigger")
                return None
            try:
                result = fn(today)
                summary = result if isinstance(result, dict) else None
                finish(name, today, "ok", summary=summary)
                return result
            except Exception as e:
                finish(name, today, "failed", error=str(e))
                raise
        return wrapper
    return decorator
