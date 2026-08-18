# -*- coding: utf-8 -*-
"""v548: margin_detail 写入修复 — SH/SZ 两路径补 short_total 列, 10 列 10 值对齐
(原 8-9 值绑 9 槽错位 → SH margin_total 恒 NULL, SZ margin_total 错位)."""

import sqlite3
import unittest.mock as mock

import pytest

from quant.data.margin import _ensure_table as ensure_tables, _sync_sse_raw


@pytest.fixture
def _conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    ensure_tables(conn)
    yield conn
    conn.close()


def test_v548_sse_path_writes_short_total(_conn):
    """SH 路径: 含 rqye (融券余额) 的响应 → short_total 与 margin_total 均写入,
    且 10 值绑 10 槽不错位."""
    rows = [
        {"stockCode": "600519", "rzmre": "1.0", "rzye": "2.0", "rzche": "3.0",
         "rqmcl": "4.0", "rqyl": "5.0", "rqye": "6.0"},
    ]
    resp = mock.Mock()
    resp.json.return_value = {"result": rows}
    with mock.patch("quant.data.margin.requests.get", return_value=resp):
        n = _sync_sse_raw("2026-08-17", _conn)
    assert n == 1
    r = _conn.execute(
        "SELECT margin_buy, margin_balance, margin_repay, short_sell_vol, "
        "short_balance, short_total, margin_total FROM margin_detail "
        "WHERE symbol='600519' AND market='SH'").fetchone()
    assert r == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, None), f"列错位: {r}"


def test_v548_sz_path_binding_shape():
    """SZ 路径: 9 值 (含 short_total) 绑 10 槽 (symbol,date,'SZ'+7 值) — 对齐."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE m (symbol, date, market, a, b, c, d, e, f, g)")
    batch = [("000001", "2026-08-17", 1.0, 2.0, None, 4.0, 5.0, 6.0, 7.0)]
    conn.executemany(
        "INSERT OR REPLACE INTO m (symbol, date, market, a, b, c, d, e, f, g) "
        "VALUES (?, ?, 'SZ', ?, ?, ?, ?, ?, ?, ?)", batch)
    r = conn.execute("SELECT * FROM m").fetchone()
    assert r == ("000001", "2026-08-17", "SZ", 1.0, 2.0, None, 4.0, 5.0, 6.0, 7.0)
    assert r[8] == 6.0 and r[9] == 7.0, f"错位: {r}"