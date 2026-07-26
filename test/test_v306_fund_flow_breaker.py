"""fund_flow 连败熔断测试 (2026-07-26 东财 IP 限流实证后添加).
背景: 东财限流期 sync_all 按 5s/只空打 5208 只股票 (~10h), 每个失败
请求都延续封禁 → 30 连败即熔断, 解封后重跑。
"""
import sqlite3

from quant.data import fund_flow


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
    conn = _mk_conn(tmp_path, n_stocks=50)
    calls = []

    def fake_sync(*args, **kwargs):
        calls.append(1)
        return 0

    monkeypatch.setattr(fund_flow, "sync_single_stock", fake_sync)
    monkeypatch.setattr(fund_flow, "_ensure_table", lambda c: None)
    monkeypatch.setattr(fund_flow, "_require_cfg", lambda k: 0)

    assert fund_flow.sync_all(max_stocks=50, conn=conn) == 0
    assert len(calls) == 30  # 未打满 50 只
    conn.close()


def test_breaker_resets_on_success(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path, n_stocks=40)
    calls = []

    def fake_sync(*args, **kwargs):
        calls.append(1)
        return 100 if len(calls) % 29 == 0 else 0  # 周期性成功 → 永不熔断

    monkeypatch.setattr(fund_flow, "sync_single_stock", fake_sync)
    monkeypatch.setattr(fund_flow, "_ensure_table", lambda c: None)
    monkeypatch.setattr(fund_flow, "_require_cfg", lambda k: 0)

    fund_flow.sync_all(max_stocks=40, conn=conn)
    assert len(calls) == 40  # 全部打完
    conn.close()
