"""jq_valuation tushare fail-fast 测试 (2026-07-27).
背景: token 档位不足时 daily_basic 报权限/日配额错误, 旧逻辑按"频"字误判为
限流, 每日期 6 次 × 62s 重试 → 14 日期空转 87min; 晚间链每天触发, 必须
识别致命错误立即终止。
"""
import os
import sqlite3
import types

import pytest

from quant.data import jq_valuation as jv


@pytest.fixture
def tushare_env(monkeypatch):
    """mock tushare 环境: token + limiter + sleep 记录。"""
    monkeypatch.setenv("TUSHARE_TOKEN", "fake-token")
    monkeypatch.setattr(jv, "_init_cache", lambda: None)
    monkeypatch.setattr(jv, "_tushare_limiter",
                        types.SimpleNamespace(wait=lambda: None))
    sleeps = []
    monkeypatch.setattr(jv.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def _patch_pro(monkeypatch, side_effect):
    import tushare as ts
    fake_pro = types.SimpleNamespace(daily_basic=lambda **kw: side_effect())
    monkeypatch.setattr(ts, "set_token", lambda t: None)
    monkeypatch.setattr(ts, "pro_api", lambda: fake_pro)


def test_permission_error_is_fatal_no_retry(tushare_env, monkeypatch):
    def raise_perm():
        raise Exception("抱歉，您没有权限访问该接口")
    _patch_pro(monkeypatch, raise_perm)
    with pytest.raises(jv.FatalSourceError):
        jv._fetch_tushare_valuation_rows("2026-07-24")
    assert tushare_env == []  # 零退避


def test_daily_quota_error_is_fatal_no_retry(tushare_env, monkeypatch):
    def raise_quota():
        raise Exception("抱歉，您每天最多访问该接口100次，今天已访问1000次")
    _patch_pro(monkeypatch, raise_quota)
    with pytest.raises(jv.FatalSourceError):
        jv._fetch_tushare_valuation_rows("2026-07-24")
    assert tushare_env == []


def test_minute_rate_limit_retries_then_gives_up(tushare_env, monkeypatch):
    def raise_rl():
        raise Exception("抱歉，您每分钟最多访问该接口1次")
    _patch_pro(monkeypatch, raise_rl)
    assert jv._fetch_tushare_valuation_rows("2026-07-24") is None
    assert tushare_env == [62] * 6  # 分钟级限流保留退避


def test_sync_range_aborts_on_fatal(tushare_env, monkeypatch, tmp_path):
    db = str(tmp_path / "m.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE daily (symbol TEXT, date TEXT)")
    conn.executemany("INSERT INTO daily VALUES ('X', ?)",
                     [("2026-07-22",), ("2026-07-23",), ("2026-07-24",)])
    conn.execute("CREATE TABLE daily_valuation (symbol TEXT, date TEXT)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(jv, "DB", db)
    calls = []

    def fake_sync_date(d, conn):
        calls.append(d)
        raise jv.FatalSourceError("没有权限")

    monkeypatch.setattr(jv, "sync_date", fake_sync_date)
    jv.sync_range("2026-07-22", "2026-07-24")
    assert calls == ["2026-07-22"]  # 首个日期致命即终止, 不再磨后续日期
