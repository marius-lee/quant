"""C5 (CODE-REVIEW 2026-08-07): multi_tf 周线前视偏差回归.

修复前: end_of_week = date - (dayofweek - 4) → 周三/周四调用时取"本周五"(未来),
用未来收盘价算周线收益 → 前视偏差.
修复后: 取 ≤ date 的最近周五 (周一~四 → 上周五), 非交易日取最近交易日, PIT 安全.
"""
import os
import sqlite3
import tempfile

import pytest

import quant.alpha.multi_tf as mtf

CLOSES = {
    "2026-07-06": 9.0, "2026-07-07": 9.3, "2026-07-08": 9.6,
    "2026-07-09": 9.8, "2026-07-10": 10.0,   # 上上周五
    "2026-07-13": 10.5, "2026-07-14": 11.0, "2026-07-15": 11.5,
    "2026-07-16": 11.7, "2026-07-17": 12.0,   # 上周五
    "2026-07-20": 12.1, "2026-07-21": 12.2, "2026-07-22": 12.3,
    "2026-07-23": 12.4, "2026-07-24": 12.5,   # 本周五
}


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch):
    td = tempfile.mkdtemp()
    db = os.path.join(td, "market.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE daily (symbol TEXT, date TEXT, close REAL)")
    for d, px in CLOSES.items():
        c.execute("INSERT INTO daily VALUES (?,?,?)", ("T1", d, px))
    c.commit()
    c.close()
    monkeypatch.setattr(mtf, "MARKET_DB", db)


def _conf():
    return mtf.MultiTimeframeConfirmer()


def test_weekday_uses_last_complete_friday():
    """周三 07-22: 周线只能用上周五收盘 → (12.0-10.0)/10.0 = 20%."""
    assert abs(_conf()._get_weekly_return("T1", "2026-07-22") - 0.20) < 1e-9


def test_thursday_cannot_see_future_friday():
    """周四 07-23: 不能用周五 07-24 收盘 (前视) → 仍 20%."""
    assert abs(_conf()._get_weekly_return("T1", "2026-07-23") - 0.20) < 1e-9


def test_friday_uses_current_week():
    """周五 07-24: 用当日收盘 → (12.5-12.0)/12.0 ≈ 4.17%."""
    assert abs(_conf()._get_weekly_return("T1", "2026-07-24") - 1 / 24) < 1e-6
