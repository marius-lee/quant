"""v424: 僵尸 running 自动清理 — pid 存活检测 + start() 自愈.

背景: restart.sh 用 pkill 强杀 orchestrator/start_all 进程 → 进行中任务
的 _tk_finish 永远不执行 → task_runs 卡 status='running' → 界面永久"运行中"
(75354 实测案例: 第二轮 weekly_eval 被 08:02 重启 pkill 后僵尸).

修复:
  1. task_log._pid_alive(pid): POSIX kill(0) 探测进程存活
  2. task_log.start(): 发现已有 running 记录但 pid 已死 → 立即标 aborted,
     不再等 grace_seconds (默认 120s, weekly_eval 43200s) 超时
  3. orchestrator._check_timeouts: 启动后扫描同样按 pid 存活清理僵尸
"""
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from quant.scheduler import task_log
from quant.scheduler import orchestrator


@pytest.fixture
def fake_db(monkeypatch, tmp_path):
    """隔离 task_runs 到临时库, 不碰生产 market.db."""
    db = tmp_path / "test_trades.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_name TEXT NOT NULL, date TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL, pid INTEGER,
            error TEXT, summary TEXT
        )
    """)
    conn.commit()
    monkeypatch.setattr(task_log, "_conn", lambda: sqlite3.connect(db))
    monkeypatch.setattr(orchestrator, "MARKET_DB", str(db))
    return lambda: sqlite3.connect(db)


def _seed(fake_db, task, date, status, pid=None, started=None):
    started = started or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn = fake_db()
    conn.execute(
        "INSERT INTO task_runs (task_name, date, started_at, finished_at, status, pid) "
        "VALUES (?, ?, ?, NULL, ?, ?)",
        (task, date, started, status, pid)
    )
    conn.commit()
    conn.close()


class TestPidAlive:
    def test_alive_self(self):
        assert task_log._pid_alive(os.getpid()) is True

    def test_dead_pid(self):
        assert task_log._pid_alive(999999) is False

    def test_none_pid(self):
        assert task_log._pid_alive(None) is True  # 旧数据无 pid → 不误判


class TestStartZombieRecovery:
    def test_start_aborts_dead_running(self, fake_db):
        """已有 running 记录但 pid 已死 → start() 标 aborted 并建新行."""
        _seed(fake_db, "weekly_eval", "2026-08-08", "running", pid=999999)
        rid = task_log.start("weekly_eval", "2026-08-08", grace_seconds=43200)
        assert rid is not None
        rows = fake_db().execute(
            "SELECT id, status, error FROM task_runs WHERE date=? ORDER BY id",
            ("2026-08-08",)
        ).fetchall()
        assert [r[1] for r in rows] == ["aborted", "running"]
        assert "进程已死 (pid=999999)" in rows[0][2]
        assert task_log.finish("weekly_eval", "2026-08-08", "ok") is None
        rows = fake_db().execute(
            "SELECT status FROM task_runs WHERE task_name='weekly_eval' AND date=? ORDER BY id",
            ("2026-08-08",)
        ).fetchall()
        assert rows[-1][0] == "ok"

    def test_start_skips_live_running(self, fake_db):
        """running 记录进程存活 → 跳过, 返回 None (原语义不变)."""
        _seed(fake_db, "signals", "2026-08-08", "running", pid=os.getpid())
        assert task_log.start("signals", "2026-08-08", grace_seconds=120) is None

    def test_start_aborts_grace_timeout(self, fake_db):
        """running 超过 grace 且 pid 存活 → 超时 aborted (原语义不变)."""
        started = (datetime.now() - timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%S")
        _seed(fake_db, "signals", "2026-08-08", "running", pid=os.getpid(), started=started)
        rid = task_log.start("signals", "2026-08-08", grace_seconds=120)
        assert rid is not None
        row = fake_db().execute(
            "SELECT status, error FROM task_runs WHERE id=?", (rid,)
        ).fetchone()
        assert row[0] == "running"


class TestCheckTimeoutsZombie:
    def test_check_timeouts_aborts_dead_pid(self, fake_db, monkeypatch):
        """_check_timeouts: running + pid 死 → 标 aborted (不等超时)."""
        import quant.scheduler.orchestrator as orch
        monkeypatch.setattr(orch, "_pid_alive", lambda pid: False)
        _seed(fake_db, "signals", "2026-08-08", "running", pid=123)
        orch._check_timeouts("2026-08-08")
        row = fake_db().execute(
            "SELECT status, error FROM task_runs WHERE task_name='signals' AND date=?",
            ("2026-08-08",)
        ).fetchone()
        assert row[0] == "aborted"
        assert "进程已死 (pid=123)" in row[1]

    def test_check_timeouts_keeps_live(self, fake_db, monkeypatch):
        """pid 存活且未超时 → 不误杀."""
        from quant.scheduler import orchestrator as orch
        monkeypatch.setattr(orch, "time", None) if False else None  # no-op
        _seed(fake_db, "signals", "2026-08-08", "running",
              pid=os.getpid(), started=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        orch._check_timeouts("2026-08-08")
        row = fake_db().execute(
            "SELECT status FROM task_runs WHERE task_name='signals' AND date=?",
            ("2026-08-08",)
        ).fetchone()
        assert row[0] == "running"