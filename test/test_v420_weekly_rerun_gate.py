"""v420: 重启补跑门控 — task_log.last_status 状态语义.

修复: _weekly_loop 重启后进程内 ran 标志丢失 → 当天重复整周评估.
门控: 当日 task_runs 最后一条状态 'ok' → 不重跑; failed/aborted/None → 允许重跑.
weekly 判定同步收紧: 任一阶段失败不得标 ok ('failed' 才触发补跑).
"""
import sqlite3
import pytest

from quant.scheduler import task_log


@pytest.fixture
def fake_trades_db(monkeypatch, tmp_path):
    """隔离 task_runs 到临时库, 不碰生产 market.db."""
    db = tmp_path / "test_trades.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_name TEXT NOT NULL, date TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL, error TEXT, summary TEXT
        )
    """)
    conn.commit()
    monkeypatch.setattr(task_log, "_conn", lambda: sqlite3.connect(db))
    return lambda: sqlite3.connect(db)


def _seed(fake_trades_db, task, date, status, error=None):
    conn = fake_trades_db()
    conn.execute(
        "INSERT INTO task_runs (task_name, date, started_at, finished_at, status, error) "
        "VALUES (?, ?, '2026-08-08T06:00:00', '2026-08-08T06:05:00', ?, ?)",
        (task, date, status, error)
    )
    conn.commit()
    conn.close()


def test_last_status_none_when_no_records(fake_trades_db):
    assert task_log.last_status("weekly_eval", "2026-08-29") is None


def test_last_status_ok_skips_rerun(fake_trades_db):
    _seed(fake_trades_db, "weekly_eval", "2026-08-29", "ok")
    assert task_log.last_status("weekly_eval", "2026-08-29") == "ok"


def test_last_status_failed_then_ok_takes_latest(fake_trades_db):
    _seed(fake_trades_db, "weekly_eval", "2026-08-29", "failed", error="boom")
    _seed(fake_trades_db, "weekly_eval", "2026-08-29", "ok")
    assert task_log.last_status("weekly_eval", "2026-08-29") == "ok"


def test_last_status_failed_allows_rerun(fake_trades_db):
    _seed(fake_trades_db, "weekly_eval", "2026-08-29", "failed", error="phase5 NameError")
    assert task_log.last_status("weekly_eval", "2026-08-29") == "failed"


def test_last_status_isolated_by_date(fake_trades_db):
    _seed(fake_trades_db, "weekly_eval", "2026-08-22", "ok")
    assert task_log.last_status("weekly_eval", "2026-08-29") is None