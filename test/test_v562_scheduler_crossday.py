"""v562: 调度页面跨天状态 — task_runs.date 是触发日, 晚间链可跨天运行.

回归 (2026-08-20): web/api/scheduler 原 WHERE date = today, 跨天后 (00:00-01:29
晚间链仍在跑) 查不到 running 记录 → 所有任务误显示"等待调度".
修复: 查近 3 天记录, 全局最新 running 且未完成 → 运行中 (与触发日无关);
今日状态判定用今日最新记录.
"""
import sys
sys.path.insert(0, '.')

import datetime as _dt
import sqlite3

import pytest

from quant.config.paths import MARKET_DB


@pytest.fixture
def isolated_runs(monkeypatch, tmp_path):
    """隔离 task_runs: 造一个临时 MARKET_DB 并 patch paths.MARKET_DB.

    返回写入函数 insert(task_name, date, status, started_at, finished_at).
    """
    tmp_db = tmp_path / "market.db"
    conn = sqlite3.connect(str(tmp_db))
    conn.execute("CREATE TABLE task_runs ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, task_name TEXT, date TEXT, "
                 "status TEXT, started_at TEXT, finished_at TEXT, error TEXT, summary TEXT)")
    conn.commit()
    monkeypatch.setattr("quant.config.paths.MARKET_DB", tmp_db)

    def insert(task_name, date, status, started_at=None, finished_at=None, error=None):
        conn.execute(
            "INSERT INTO task_runs (task_name, date, status, started_at, finished_at, error) "
            "VALUES (?,?,?,?,?,?)",
            (task_name, date, status, started_at, finished_at, error))
        conn.commit()
    yield insert
    conn.close()


def _api_scheduler_json():
    """直接调用 Flask test client (web 已注册路由)."""
    import web.app as app_mod
    app_mod.app.config["TESTING"] = True
    client = app_mod.app.test_client()
    resp = client.get("/api/scheduler")
    assert resp.status_code == 200
    return resp.get_json()


def _task_state(data, key):
    for t in data["data"]["tasks"]:
        if t["key"] == key:
            return t
    raise AssertionError(f"task {key} not in scheduler page")


class TestCrossDayRunning:
    """核心回归: 昨天 running 未完成 → 今天仍显示运行中."""

    def test_yesterday_running_shows_running_today(self, isolated_runs):
        # 昨日晚间链 19:00 触发, 至今未完成 (跨天 running)
        isolated_runs("daily_data", "2026-08-19", "running",
                      started_at="2026-08-19T19:00:25")
        data = _api_scheduler_json()
        st = _task_state(data, "daily_data")
        assert st["status"] == "running", f"跨天 running 应显示运行中, got {st['status']}"

    def test_yesterday_finished_shows_waiting_today(self, isolated_runs):
        # 昨日已 ok → 今日尚未触发 → 等待调度
        isolated_runs("daily_data", "2026-08-19", "ok",
                      started_at="2026-08-19T19:00:25",
                      finished_at="2026-08-20T01:29:47")
        data = _api_scheduler_json()
        st = _task_state(data, "daily_data")
        assert st["status"] == "pending", f"昨日 ok 今日未跑应显示等待调度, got {st['status']}"

    def test_today_ok_wins_over_yesterday_running(self, isolated_runs):
        # 昨日 running (残留旧行) + 今日 ok → 应显示今日已执行
        isolated_runs("signals", "2026-08-19", "running",
                      started_at="2026-08-19T08:30:00")
        isolated_runs("signals", "2026-08-20", "ok",
                      started_at="2026-08-20T08:30:00",
                      finished_at="2026-08-20T08:30:02")
        data = _api_scheduler_json()
        st = _task_state(data, "signals")
        assert st["status"] == "success", f"今日 ok 应覆盖昨日 running, got {st['status']}"