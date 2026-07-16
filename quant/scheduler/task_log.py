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


def _conn():
    """打开 market.db 连接 (WAL 模式 + 忙等待)."""
    c = sqlite3.connect(MARKET_DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _ensure_table():
    """幂等建表."""
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
                error      TEXT,
                summary    TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_date ON task_runs(date, task_name)")
        conn.commit()
    finally:
        conn.close()


# ── 模块加载时自动建表 ──
_ensure_table()


def start(task_name: str, date: str) -> int:
    """任务启动时调用。返回插入的 row id。
    
    Args:
        task_name: 'signals' | 'execute' | 'monitor' | 'attribution' | 'weekly_eval'
        date: '2026-07-15'
    """
    conn = _conn()
    try:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        # 自动清理同任务同日期的旧 running 僵尸行 → aborted
        conn.execute(
            "UPDATE task_runs SET status='aborted', finished_at=?, error='上次运行未正常结束 (auto-abort)' "
            "WHERE task_name=? AND date=? AND status='running'",
            (now, task_name, date)
        )
        cur = conn.execute(
            "INSERT INTO task_runs (task_name, date, started_at, status) VALUES (?, ?, ?, 'running')",
            (task_name, date, now)
        )
        conn.commit()
        return cur.lastrowid
    finally:
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
            "SELECT id FROM task_runs"
            " WHERE task_name = ? AND date = ? AND status = 'running'"
            " ORDER BY id DESC LIMIT 1",
            (task_name, date)
        ).fetchone()
        if row is None:
            import logging
            logging.getLogger(__name__).warning(
                f"finish({task_name}, {date}) — no running row found, "
                f"possibly already updated by another process"
            )
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
