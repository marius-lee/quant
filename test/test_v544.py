# -*- coding: utf-8 -*-
"""v544: financial_anomaly NaN 传染修复 — 子因子值为 NaN 时跳过而非污染整只股票,
归一化 z/count (平均偏差) 保证跨期口径一致. 根因: sina 数据源 2020-2024 缺
营业成本/管理费用字段 → 表内 NaN → 旧代码 z += NaN 传染 → 恒空 (2025 起自愈)."""

import ast
import textwrap

import pandas as pd
import pytest

from quant.factor.compute.fundamental import compute_financial_anomaly


@pytest.fixture(scope="module")
def _aux_2020():
    import sqlite3

    from quant.factor.compute._preload import preload_aux_data_chunk, slice_aux_for_date

    syms = [
        r[0]
        for r in sqlite3.connect("quant/data/market.db").execute(
            "SELECT DISTINCT symbol FROM daily WHERE date='2020-06-30' ORDER BY symbol LIMIT 500"
        )
    ]
    aux_full = preload_aux_data_chunk(syms, "2020-06-01", "2020-06-30")
    return slice_aux_for_date(aux_full, "2020-06-30"), syms


@pytest.fixture(scope="module")
def _aux_2025():
    import sqlite3

    from quant.factor.compute._preload import preload_aux_data_chunk, slice_aux_for_date

    syms = [
        r[0]
        for r in sqlite3.connect("quant/data/market.db").execute(
            "SELECT DISTINCT symbol FROM daily WHERE date='2025-06-30' ORDER BY symbol LIMIT 500"
        )
    ]
    aux_full = preload_aux_data_chunk(syms, "2025-06-01", "2025-06-30")
    return slice_aux_for_date(aux_full, "2025-06-30"), syms


def test_v544_source_uses_notna_and_mean_norm():
    src = textwrap.dedent(ast.get_source_segment(
        open("quant/factor/compute/fundamental.py", encoding="utf-8").read(),
        ast.parse(open("quant/factor/compute/fundamental.py", encoding="utf-8").read()).body[0],
    )) if False else open("quant/factor/compute/fundamental.py", encoding="utf-8").read()
    body = src.split("def compute_financial_anomaly")[1]
    assert "pd.notna(inv_g)" in body and "pd.notna(rev_g)" in body, "子因子 NaN 必须跳过 (不传染)"
    assert "pd.notna(adm_g)" in body, "admin 子因子 NaN 必须跳过"
    assert "scores[sym] = z / count" in body, "归一化必须为平均偏差 z/count (跨期口径一致)"
    assert "z / count * 4" not in body, "旧放大归一化已移除"


def test_v544_nonempty_when_cost_cols_missing(_aux_2020):
    aux, syms = _aux_2020
    r = compute_financial_anomaly(pd.DataFrame(index=syms), "2020-06-30", aux=aux)
    assert r is not None and not r.dropna().empty, "2020 期 (历史缺 2 子因子) 必须非空 (NaN 传染修复)"
    assert len(r.dropna()) > 400, f"覆盖应 >400 (实际 {len(r.dropna())})"


def test_v544_full_4_subfactors(_aux_2025):
    aux, syms = _aux_2025
    latest = aux["financial_income"]["stat_date"].max()
    latest_rows = aux["financial_income"][aux["financial_income"]["stat_date"] == latest]
    assert latest_rows["operating_cost"].isna().mean() < 0.1, "前置: 最新期营业成本应齐全"
    r = compute_financial_anomaly(pd.DataFrame(index=syms), "2025-06-30", aux=aux)
    assert r is not None and not r.dropna().empty
    assert r.dropna().std() > 0.5, "zscore 应有效离散"