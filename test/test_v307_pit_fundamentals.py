"""严格 PIT 估值测试 — 审计 P0-4 (2026-07-26).
修复前: get_fundamentals(date) 在 daily_valuation 覆盖外回退 stocks 快照
(最新 PE/PB/总市值) → 历史日期物化行前视 (07-06..07-24 实证污染)。
修复后: 只认 ≤ date 的最近 daily_valuation 交易日, 覆盖外 → NaN。
"""
import sqlite3

import pandas as pd

from quant.data.store import DataStore

_SNAPSHOT = ("INSERT OR REPLACE INTO stocks "
             "(symbol, name, market, list_date, industry, pe, pe_ttm, pb,"
             " total_mv, roe) VALUES "
             "('000001','PA','SZ','2000-01-01','BANK', 999.0, 999.0, 99.0,"
             " 999e8, 0.99)")


def _mk(path):
    ds = DataStore(db_path=str(path))
    conn = ds._connect()
    for col in ["pe", "pe_ttm", "pb", "ps_ttm", "pcf_ttm", "total_mv",
                "roe", "high_52w", "eps", "bvps"]:
        try:
            conn.execute(f"ALTER TABLE stocks ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.execute(_SNAPSHOT)
    conn.commit()
    return ds


def _seed(path):
    ds = _mk(path)
    conn = ds._connect()
    conn.executemany(
        "INSERT OR REPLACE INTO daily_valuation "
        "(symbol, date, pe_ttm, pb, ps_ttm, pcf_ttm, market_cap) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("000001", "2026-06-30", 10.0, 1.0, None, None, 100.0),
         ("000001", "2026-07-03", 11.0, 1.1, None, None, 110.0)])
    conn.execute("INSERT OR REPLACE INTO daily VALUES "
                 "('000001','2026-07-24', 1,1,1, 12.0, 1,1,1)")
    conn.commit()
    return ds


def test_pit_uses_latest_valuation_le_date(tmp_path):
    """07-24 请求 → 拿到 07-03 (≤ date 最近) 估值, 不是 999 快照。"""
    ds = _seed(tmp_path / "m.db")
    df = ds.get_fundamentals(["000001"], date="2026-07-24")
    row = df.loc["000001"]
    assert row["pe_ttm"] == 11.0 and row["pe"] == 11.0
    assert row["pb"] == 1.1
    assert abs(row["total_mv"] - 110.0 * 1e8) < 1  # 亿元→元
    assert row["roe"] is None or pd.isna(row["roe"]) or 0 < row["roe"] < 1
    ds.close()


def test_pit_no_snapshot_fallback(tmp_path):
    """valuation 表空 → 全部 NaN (诚实缺数据), 不回退 999 快照。"""
    ds = _mk(tmp_path / "m2.db")
    df = ds.get_fundamentals(["000001"], date="2026-07-24")
    row = df.loc["000001"]
    assert pd.isna(row["pe_ttm"]) and pd.isna(row["pb"]) and pd.isna(row["total_mv"])
    assert pd.isna(row["roe"])
    ds.close()


def test_no_date_keeps_snapshot_live_path(tmp_path):
    """date=None (实盘快照路径) 行为不变。"""
    ds = _seed(tmp_path / "m3.db")
    df = ds.get_fundamentals(["000001"])  # 无 date
    assert df.loc["000001"]["pe"] == 999.0
    ds.close()
