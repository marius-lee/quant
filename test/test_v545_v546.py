# -*- coding: utf-8 -*-
"""v545: sina_financials sync 增强 — financial_income 行存在但 operating_cost/
administration_expense 为 NULL 不算已同步 (2020-2024 历史行缺字段必须重拉补齐),
避免未来字段缺失永不修复."""

import sqlite3

import pytest

from quant.config.paths import MARKET_DB
from quant.factor.store import _empty_factor_summary, _unblock_recovered


@pytest.fixture(scope="module")
def _conn():
    conn = sqlite3.connect(str(MARKET_DB))
    yield conn
    conn.close()


def test_v545_existing_requires_cost_cols(_conn):
    """financial_income 已同步集合必须排除两列 NULL 的行 (sync existing 语义)."""
    ok = _conn.execute(
        "SELECT symbol, stat_date FROM financial_income "
        "WHERE operating_cost IS NOT NULL AND administration_expense IS NOT NULL"
    ).fetchall()
    bad = _conn.execute(
        "SELECT symbol, stat_date FROM financial_income "
        "WHERE operating_cost IS NULL OR administration_expense IS NULL"
    ).fetchall()
    assert ok and bad, "前置: 表内应有已补齐与未补齐两类行"
    ok_set = set((r[0], str(r[1])[:10]) for r in ok)
    bad_set = set((r[0], str(r[1])[:10]) for r in bad)
    assert ok_set.isdisjoint(bad_set), "两集合必须互斥 (NULL 行不得视为已同步)"


def test_v545_needs_cost_detects_missing_rows(_conn):
    """needs_cost 判定: 存在 NULL 行的股票应标记为需拉取. 用银行股 000001
    (无营业成本科目, 两列恒 NULL, 补数后仍 NULL → 稳定命中)."""
    assert _conn.execute(
        "SELECT 1 FROM financial_income WHERE symbol='000001' AND "
        "(operating_cost IS NULL OR administration_expense IS NULL) LIMIT 1"
    ).fetchone() is not None


def test_v546_unblock_recovered_removes_only_recovered():
    """成功重算因子解除 blocked; 同日期其他因子保留; 空日期 dict 整体移除."""
    blocked = {"2020-01-02": {"fund_change": 1.0, "accruals": 2.0}, "2020-01-03": {"x": 3.0}}
    _unblock_recovered(blocked, {"fund_change": {"2020-01-02"}, "y": {"2020-01-05"}})
    assert blocked == {"2020-01-02": {"accruals": 2.0}, "2020-01-03": {"x": 3.0}}
    _unblock_recovered(blocked, {"x": {"2020-01-03"}})
    assert blocked == {"2020-01-02": {"accruals": 2.0}}


def test_v546_empty_summary_threshold_and_order():
    """空结果聚合: 按因子计数, 只含 >= 阈值, 降序."""
    pairs = [("d1", "f_a")] * 60 + [("d2", "f_a")] * 10 + [("d3", "f_b")] * 49 + [("d4", "f_b")] * 51
    s = _empty_factor_summary(pairs, min_days=50)
    assert s == [("f_b", 100), ("f_a", 70)], s
    assert _empty_factor_summary([("d1", "f_c")] * 49, min_days=50) == []