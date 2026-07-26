"""数据新鲜度 watchdog 测试 — 审计 P0-2 (2026-07-26).
背景: fund_flow 停滞 5 个月 / margin 17 天无告警。freshness.check_freshness
按表 SLO 判定 stale, 供 daily_data 告警。
"""
import sqlite3

from quant.data.freshness import check_freshness, unavailable_factors, SLOS


def _mk_db(path, tables: dict):
    """tables: {table_name: [date, ...]} — 建表并插入 date 列值。"""
    conn = sqlite3.connect(path)
    for t, dates in tables.items():
        conn.execute(f"CREATE TABLE {t} (symbol TEXT, date TEXT)")
        conn.executemany(f"INSERT INTO {t} VALUES ('X', ?)", [(d,) for d in dates])
    conn.commit()
    conn.close()


def test_fresh_table_not_stale(tmp_path):
    db = str(tmp_path / "m.db")
    _mk_db(db, {"daily": ["2026-07-26"], "fund_flow": ["2026-07-24"],
                "margin_detail": ["2026-07-23"], "daily_valuation": ["2026-07-24"],
                "adj_factor": ["2026-07-20"]})
    res = {r["table"]: r for r in check_freshness("2026-07-26", db)}
    assert not any(r["stale"] for r in res.values())


def test_stale_table_flagged(tmp_path):
    """fund_flow 停滞 5 个月场景: 2026-02-27 vs 2026-07-26 → stale。"""
    db = str(tmp_path / "m.db")
    _mk_db(db, {"daily": ["2026-07-26"], "fund_flow": ["2026-02-27"],
                "margin_detail": ["2026-07-09"], "daily_valuation": ["2026-07-24"],
                "adj_factor": ["2026-07-20"]})
    res = {r["table"]: r for r in check_freshness("2026-07-26", db)}
    assert res["fund_flow"]["stale"] and res["fund_flow"]["lag_days"] == 149
    assert res["margin_detail"]["stale"] and res["margin_detail"]["lag_days"] == 17
    assert not res["daily"]["stale"]
    assert not res["daily_valuation"]["stale"]


def test_missing_table_is_stale(tmp_path):
    db = str(tmp_path / "m.db")
    _mk_db(db, {"daily": ["2026-07-26"]})
    res = {r["table"]: r for r in check_freshness("2026-07-26", db)}
    assert res["margin_detail"]["stale"] and res["margin_detail"]["max_date"] is None
    assert res["margin_detail"]["lag_days"] is None


def test_slos_cover_factor_relevant_tables():
    """因子依赖的关键表都应有 SLO (防漏配)。"""
    for t in ("daily", "fund_flow", "margin_detail", "daily_valuation", "adj_factor"):
        assert t in SLOS


def test_unavailable_factors_pruning(tmp_path):
    """P0-3: 源表 stale → 其衍生因子进裁剪集; 健康表因子不受影响。"""
    db = str(tmp_path / "m.db")
    _mk_db(db, {"daily": ["2026-07-26"], "fund_flow": ["2026-02-27"],
                "margin_detail": ["2026-07-24"], "daily_valuation": ["2026-07-24"],
                "adj_factor": ["2026-07-20"]})
    un = unavailable_factors("2026-07-26", db)
    # fund_flow 停滞 5 个月 → fund_flow_3m + main_flow_ratio 应被裁剪
    assert "fund_flow_3m" in un and "main_flow_ratio" in un
    # margin 健康 → 两融因子不在裁剪集
    assert "short_interest" not in un and "margin_balance_chg" not in un


def test_unavailable_factors_all_healthy(tmp_path):
    db = str(tmp_path / "m.db")
    _mk_db(db, {"daily": ["2026-07-26"], "fund_flow": ["2026-07-24"],
                "margin_detail": ["2026-07-23"], "daily_valuation": ["2026-07-24"],
                "adj_factor": ["2026-07-20"]})
    assert unavailable_factors("2026-07-26", db) == set()
