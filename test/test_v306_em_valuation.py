"""em_valuation 东财估值抓取测试 (2026-07-27, test-v306).
背景: tushare 档位不足 / JQ 窗口外 → 东财 datacenter 承接近 3 个月估值。
"""
import sqlite3
import types

import pytest

from quant.data import em_valuation as ev


def _mk_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "m.db"))
    conn.execute(
        "CREATE TABLE daily_valuation (symbol TEXT, date TEXT, pe_ttm REAL, "
        "pb REAL, ps_ttm REAL, pcf_ttm REAL, market_cap REAL, "
        "turnover_rate REAL, source TEXT, PRIMARY KEY (symbol, date))")
    conn.execute("CREATE TABLE daily (symbol TEXT, date TEXT)")
    return conn


def _page(rows, pages, pageno):
    return types.SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"success": True, "message": "ok",
                      "result": {"pages": pages, "data": rows, "count": len(rows)}},
    )


def _row(code, suffix, pe=10.0, pb=1.5):
    return {"SECURITY_CODE": code, "SECUCODE": f"{code}.{suffix}",
            "PE_TTM": pe, "PB_MRQ": pb, "PS_TTM": 2.0,
            "PCF_OCF_TTM": 5.0, "TOTAL_MARKET_CAP": 1e9,
            "TRADE_DATE": "2026-07-24 00:00:00"}


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(ev, "_require_cfg", lambda k: 0)
    monkeypatch.setattr(ev.time, "sleep", lambda s: None)


def test_fetch_paginates_all_pages(fast, monkeypatch):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params["pageNumber"])
        if params["pageNumber"] == 1:
            return _page([_row("600519", "SH")], pages=2, pageno=1)
        return _page([_row("000001", "SZ")], pages=2, pageno=2)

    monkeypatch.setattr(ev.requests, "get", fake_get)
    rows = ev._fetch_date("2026-07-24")
    assert calls == [1, 2]
    assert {r["SECUCODE"] for r in rows} == {"600519.SH", "000001.SZ"}


def test_insert_maps_fields_and_filters_bj(fast, tmp_path):
    conn = _mk_db(tmp_path)
    rows = [
        _row("600519", "SH", pe=30.5, pb=10.2),
        _row("830799", "BJ"),          # BJ 排除
        {**_row("300750", "SZ"), "PE_TTM": None},  # 空字段跳过该列
        {"SECUCODE": "12345.SH"},      # 非法代码 + 无字段 → 跳过
    ]
    n = ev._insert_rows(conn, rows, "2026-07-24")
    assert n == 2
    got = {r[0]: r for r in conn.execute(
        "SELECT symbol, pe_ttm, pb, source FROM daily_valuation").fetchall()}
    assert got["600519"][1] == 30.5 and got["600519"][3] == "eastmoney"
    assert "830799" not in got
    # 空 PE → 该列不写入 (保持 NULL)
    assert got["300750"][1] is None and got["300750"][2] == 1.5
    conn.close()


def test_sync_range_breaker_aborts_after_3_consecutive_failures(fast, monkeypatch, tmp_path):
    conn = _mk_db(tmp_path)
    for d in ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24"]:
        conn.execute("INSERT INTO daily VALUES ('X', ?)", (d,))
    conn.commit()

    def boom(date_str, conn=None):
        raise RuntimeError("source down")

    monkeypatch.setattr(ev, "sync_date", boom)
    assert ev.sync_range("2026-07-20", "2026-07-24", conn=conn) == 0
    conn.close()


def test_sync_range_skips_synced_dates(fast, monkeypatch, tmp_path):
    conn = _mk_db(tmp_path)
    for d in ["2026-07-23", "2026-07-24"]:
        conn.execute("INSERT INTO daily VALUES ('X', ?)", (d,))
    conn.execute(
        "INSERT INTO daily_valuation (symbol, date, pe_ttm) VALUES ('600519', '2026-07-23', 30)")
    conn.commit()
    done = []
    monkeypatch.setattr(ev, "sync_date", lambda d, conn=None: done.append(d) or 100)
    ev.sync_range("2026-07-23", "2026-07-24", conn=conn)
    assert done == ["2026-07-24"]
    conn.close()
