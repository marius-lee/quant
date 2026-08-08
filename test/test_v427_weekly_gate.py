"""复活门控修复测试 — v427: 整体周评估在"当前已 ok"时 shouldn't 复跑."""

import sqlite3
from datetime import datetime

import pytest

from quant.scheduler import task_log


@pytest.fixture
def fake_db(monkeypatch, tmp_path):
    db = tmp_path / "test_trades.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_name TEXT NOT NULL, date TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL, pid INTEGER,
            error TEXT, summary TEXT
        )
    """)
    conn.commit()
    monkeypatch.setattr(task_log, "_conn", lambda: sqlite3.connect(db))
    return lambda: sqlite3.connect(db)


def _seed(fake_db, task, date, status):
    conn = fake_db()
    conn.execute(
        "INSERT INTO task_runs (task_name, date, started_at, finished_at, status, pid) "
        "VALUES (?, ?, ?, NULL, ?, ?)",
        (task, date, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), status, 1)
    )
    conn.commit()
    conn.close()


class TestAnyOk:
    def test_found_when_ok_exists(self, fake_db):
        _seed(fake_db, "weekly_eval", "2026-08-08", "ok")
        assert task_log.any_ok("weekly_eval", "2026-08-08") is True

    def test_not_blocked_by_later_aborted(self, fake_db):
        """关键: ok 行更早, 后来 zombie aborted 行遮蔽 last_status → any_ok 仍 True."""
        _seed(fake_db, "weekly_eval", "2026-08-08", "ok")
        _seed(fake_db, "weekly_eval", "2026-08-08", "aborted")
        assert task_log.any_ok("weekly_eval", "2026-08-08") is True

    def test_false_for_aborted_only(self, fake_db):
        _seed(fake_db, "weekly_eval", "2026-08-08", "aborted")
        assert task_log.any_ok("weekly_eval", "2026-08-08") is False

    def test_false_for_other_date(self, fake_db):
        _seed(fake_db, "weekly_eval", "2026-08-08", "ok")
        assert task_log.any_ok("weekly_eval", "2026-08-09") is False

    def test_false_when_no_rows(self, fake_db):
        assert task_log.any_ok("weekly_eval", "2026-08-08") is False