"""fund_flow 连败熔断测试 (2026-07-26 东财 IP 限流实证后添加).
背景: 东财限流期 sync_all 按 5s/只空打 5208 只股票 (~10h), 每个失败
请求都延续封禁 → 30 连败即熔断, 解封后重跑。
"""
import sqlite3

from quant.data import fund_flow


import pytest
@pytest.fixture(autouse=True)
def reset_fund_flow_flag():
    """v414: 每个测试前重置 API 不可用标志."""
    fund_flow._UNAVAILABLE = False


def _mk_conn(tmp_path, n_stocks=50):
    conn = sqlite3.connect(str(tmp_path / "m.db"))
    conn.execute("CREATE TABLE stocks (symbol TEXT, market TEXT, total_mv REAL)")
    conn.executemany(
        "INSERT INTO stocks VALUES (?, 'SH', ?)",
        [(f"600{i:03d}", 1000.0 - i) for i in range(n_stocks)],
    )
    conn.commit()
    return conn


def test_breaker_aborts_after_30_consecutive_failures(tmp_path, monkeypatch):
    """v414: 5连败+0成功 → API unavailable (早停), 跳过到30连败检查."""
    conn = _mk_conn(tmp_path, n_stocks=50)
    calls = []

    def fake_sync(*args, **kwargs):
        calls.append(1)
        return 0

    monkeypatch.setattr(fund_flow, "sync_single_stock", fake_sync)
    monkeypatch.setattr(fund_flow, "_ensure_table", lambda c: None)
    monkeypatch.setattr(fund_flow, "_require_cfg", lambda k: 0)

    assert fund_flow.sync_all(max_stocks=50, conn=conn) == 0
    assert len(calls) == 5  # v414: 5连败+0成功 → API unavailable, 不再等到30
    conn.close()


def test_breaker_resets_on_success(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path, n_stocks=40)
    calls = []

    def fake_sync(*args, **kwargs):
        calls.append(1)
        return 100 if len(calls) % 3 == 0 else 0  # v414: 每3次成功, ok>0 避免5连败断路器

    monkeypatch.setattr(fund_flow, "sync_single_stock", fake_sync)
    monkeypatch.setattr(fund_flow, "_ensure_table", lambda c: None)
    monkeypatch.setattr(fund_flow, "_require_cfg", lambda k: 0)

    fund_flow.sync_all(max_stocks=40, conn=conn)
    assert len(calls) == 40  # 全部打完
    conn.close()
