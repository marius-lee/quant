"""v479 数据健康闭环测试 — 注册表 / 完整性审计 / 自动补拉修复.

覆盖: registry 完整性、4 类审计规则 (freshness/gap_dates/coverage/total_rows/
custom)、repair_and_reaudit 闭环、data_audit 表流转、freshness API 兼容.
"""
import sqlite3

import pytest

from quant.data.table_registry import (
    REGISTRY, TableSpec, rollback_specs, weekly_full_specs, factors_for_tables,
)
from quant.data.data_health import (
    audit_table, audit_all, failed_tables_on, last_ok_check,
    consecutive_failures, repair_and_reaudit, ensure_audit_table,
)
from quant.data import freshness

DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
TODAY = "2026-08-10"  # 周一 (08-08/09 周末)


@pytest.fixture()
def db(tmp_path):
    """tmp 市场库: daily 5 交易日 × 100 行."""
    p = tmp_path / "market.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE daily (date TEXT, symbol TEXT, PRIMARY KEY(date, symbol))")
    for d in DAYS:
        c.executemany("INSERT INTO daily VALUES (?, ?)",
                      [(d, f"{i:06d}") for i in range(100)])
    c.execute("CREATE TABLE fund_flow (date TEXT, symbol TEXT, amount REAL)")
    c.execute("CREATE TABLE margin_detail (date TEXT, symbol TEXT, amount REAL)")
    c.execute("CREATE TABLE dividend (symbol TEXT, end_date TEXT, ex_date TEXT, cash_div REAL)")
    c.execute("CREATE TABLE stocks (symbol TEXT, total_shares REAL, list_date TEXT)")
    c.commit()
    c.close()
    return str(p)


# ── 1. 注册表完整性 ──────────────────────────────────────────────────
def test_registry_covers_all_tables():
    assert set(REGISTRY) >= {"daily", "daily_valuation", "adj_factor", "fund_flow",
                             "margin_detail", "lhb_detail", "limit_up_pool",
                             "limit_down_pool", "dividend", "stocks",
                             "benchmark_daily"}
    assert len(rollback_specs()) >= 6
    assert {s.table for s in weekly_full_specs()} >= {"dividend", "stocks"}


def test_registry_syncs_callable():
    """rollback/weekly_full 表均有可调用同步函数 (不真正调用)."""
    for spec in rollback_specs() + weekly_full_specs():
        assert spec.sync_main is not None, f"{spec.table} 缺 sync_main"
        assert callable(spec.sync_main)
    assert REGISTRY["daily"].sync_main is None  # primary 主流程


def test_factors_mapping():
    assert factors_for_tables({"margin_detail"}) >= {"margin_buy_ratio"}
    assert factors_for_tables({"dividend"}) == {"dividend_yield"}
    assert factors_for_tables({"stocks"}) == {"sue"}
    assert factors_for_tables({"fund_flow"}) >= {"fund_flow_3m"}


# ── 2. 审计规则 ──────────────────────────────────────────────────────
def _spec(**kw):
    base = dict(table="fund_flow", date_col="date", mode="rollback",
                sync_main=lambda s, e: 0, window_days=14,
                min_rows_per_day=50, slo_days=2)
    base.update(kw)
    return TableSpec(**base)


def test_audit_freshness_stale(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO fund_flow SELECT date, symbol, 1.0 FROM daily")
    conn.commit()
    # max=08-07, today=08-10 → lag=3 > slo=2
    results = audit_table(conn, _spec(), TODAY)
    rules = {r[0]: r[1] for r in results}
    assert rules["freshness"] == "fail"
    conn.close()


def test_audit_gap_dates(db):
    conn = sqlite3.connect(db)
    # 只补最近 3 天 → 缺 08-03/04
    for d in DAYS[2:]:
        conn.execute("INSERT INTO fund_flow SELECT date, symbol, 1.0 FROM daily WHERE date=?",
                     (d,))
    conn.commit()
    rules = {r[0]: r[1] for r in audit_table(conn, _spec(), TODAY)}
    assert rules["gap_dates"] == "fail"
    assert "缺 2" in [r[2] for r in audit_table(conn, _spec(), TODAY) if r[0] == "gap_dates"][0]
    conn.close()


def test_audit_coverage_and_total_rows(db):
    conn = sqlite3.connect(db)
    # 全覆盖但每日只有 10 行 (< min 50) → coverage fail
    conn.execute("INSERT INTO fund_flow SELECT date, symbol, 1.0 FROM daily LIMIT 10")
    conn.commit()
    rules = {r[0]: r[1] for r in audit_table(conn, _spec(), TODAY)}
    assert rules["coverage"] == "fail"
    conn.execute("DELETE FROM fund_flow")
    spec = _spec(min_total_rows=500)
    conn.commit()
    rules2 = {r[0]: r[1] for r in audit_table(conn, spec, TODAY)}
    assert rules2["total_rows"] == "fail"
    conn.close()


def test_audit_custom_stocks_coverage(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO stocks VALUES ('000001', NULL, NULL)")
    conn.execute("INSERT INTO stocks VALUES ('600000', 1e9, '2000-01-01')")
    conn.commit()
    rules = {r[0]: r[1] for r in audit_table(conn, REGISTRY["stocks"], TODAY)}
    assert rules["custom"] == "fail"  # 覆盖 50% < 99%
    for i in range(1, 101):
        conn.execute("INSERT INTO stocks VALUES (?, 1e9, '2000-01-01')", (f"{i:06d}",))
    conn.commit()
    rules = {r[0]: r[1] for r in audit_table(conn, REGISTRY["stocks"], TODAY)}
    assert rules["custom"] == "ok"
    conn.close()


def test_audit_ok_all_green(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO fund_flow SELECT date, symbol, 1.0 FROM daily")
    conn.commit()
    rules = {r[0]: r[1] for r in audit_table(conn, _spec(slo_days=10), TODAY)}
    assert all(v == "ok" for v in rules.values())
    conn.close()


# ── 3. 审计 → 补拉 → 复审闭环 ───────────────────────────────────────
def test_repair_and_reaudit_loop(db, monkeypatch):
    """失败表 → 补拉(假 sync 写数据) → 复审全绿 → repaired 标记."""
    calls = {}

    def fake_sync(start, end):
        calls["args"] = (start, end)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO fund_flow SELECT date, symbol, 1.0 FROM daily")
        conn.commit()
        conn.close()
        return 500

    monkeypatch.setitem(REGISTRY, "fund_flow",
                        _spec(sync_main=fake_sync, slo_days=10))
    repaired, still = repair_and_reaudit(TODAY, ["fund_flow"], db_path=db)
    assert repaired == ["fund_flow"]
    assert still == []
    assert calls["args"] == ("2026-07-27", TODAY)  # window 14 天
    # data_audit 表留痕 (fund_flow 审计产生 3 规则: freshness/gap_dates/coverage)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM data_audit WHERE table_name='fund_flow' "
                     "AND repaired=1 AND status='ok'").fetchone()[0]
    assert n >= 3
    conn.close()


def test_repair_still_failed_on_sync_error(db, monkeypatch):
    def bad_sync(start, end):
        raise RuntimeError("源挂了")

    monkeypatch.setitem(REGISTRY, "fund_flow", _spec(sync_main=bad_sync))
    repaired, still = repair_and_reaudit(TODAY, ["fund_flow"], db_path=db)
    assert repaired == []
    assert still == ["fund_flow"]
    assert failed_tables_on(TODAY, db_path=db) == ["fund_flow"]


def test_consecutive_failures_and_last_ok(db, monkeypatch):
    monkeypatch.setitem(REGISTRY, "fund_flow", _spec(sync_main=lambda s, e: 0))
    from datetime import datetime as _dt, timedelta as _td
    # 连续三天失败 (相对真实今天 — consecutive_failures 以 now 为基准)
    for i in (3, 2, 1):
        d = (_dt.now() - _td(days=i)).strftime("%Y-%m-%d")
        repair_and_reaudit(d, ["fund_flow"], db_path=db)
    assert consecutive_failures("fund_flow", days=5, db_path=db) == 3
    assert last_ok_check("fund_flow", db_path=db) is None
    # 修复一天
    def good_sync(s, e):
        c = sqlite3.connect(db)
        c.execute("INSERT INTO fund_flow SELECT date, symbol, 1.0 FROM daily")
        c.commit(); c.close()
        return 500
    monkeypatch.setitem(REGISTRY, "fund_flow", _spec(sync_main=good_sync, slo_days=10))
    repaired, _ = repair_and_reaudit(TODAY, ["fund_flow"], db_path=db)
    assert repaired == ["fund_flow"]
    assert last_ok_check("fund_flow", db_path=db) is not None


def test_audit_all_writes_every_table(db, monkeypatch):
    """audit_all 覆盖全部注册表且写 data_audit (不抛错)."""
    def noop_sync(s=None, e=None):
        return 0
    for spec in list(REGISTRY.values()):
        if spec.sync_main is not None and spec.mode != "primary":
            monkeypatch.setitem(REGISTRY, spec.table,
                                TableSpec(**{**spec.__dict__, "sync_main": noop_sync}))
    result = audit_all(TODAY, db_path=db)
    assert set(result) == set(REGISTRY)
    assert all(rules for rules in result.values())  # 每表至少 1 规则 (事件型无 freshness)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM data_audit WHERE date=?", (TODAY,)).fetchone()[0]
    conn.close()
    assert n >= len(REGISTRY)  # 每表至少 1 规则


# ── 4. freshness API 兼容 (v479 迁至注册表) ─────────────────────────
def test_freshness_slos_from_registry():
    assert freshness.SLOS["daily"] == 4
    assert freshness.SLOS["dividend"] is None  # 事件型不判
    assert freshness.TABLE_TO_FACTORS["margin_detail"] >= {"margin_buy_ratio"}


def test_unavailable_factors_aggregates(db):
    # margin_detail 滞后 7 天 (> slo=6) → 其因子被裁剪; dividend 事件型不裁剪
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO margin_detail SELECT date, symbol, 1.0 FROM daily WHERE date='2026-08-03'")
    conn.execute("INSERT INTO stocks VALUES ('000001', 1e9, '2020-01-01')")
    conn.commit()
    uf = freshness.unavailable_factors(TODAY, db_path=db)
    assert isinstance(uf, set)
    assert "margin_buy_ratio" in uf  # margin_detail lag=7 > slo=6
    assert "dividend_yield" not in uf  # 事件型不判新鲜度
    conn.close()


def test_missing_table_detected(db):
    conn = sqlite3.connect(db)
    rules = {r[0]: r[1] for r in audit_table(conn, _spec(table="no_such_table"), TODAY)}
    assert rules["freshness"] == "fail"
    conn.close()
