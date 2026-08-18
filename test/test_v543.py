# v543 tests — compute_fund_change symbol key 修复:
# cece5a6 (07-17 aux 重构) 把 SQL 直查改 iterrows 时, 用行号当 scores 的 key
# (iterrows 的 index 是行号, symbol 是普通列) → reindex(symbols) 全 NaN →
# 恒空 → 245 天 blocked (2026-08-19 05:40 force 物化 seg_0 18 交易日全空实证).
# 修复: key 改用 row["symbol"] → 修复后 500 只复刻 500/500 非 NaN.
import sqlite3

import pandas as pd
import pytest

from quant.factor.compute._preload import preload_aux_data_chunk, slice_aux_for_date
from quant.factor.compute.price._event import compute_fund_change


def _build(date: str, n: int = 500) -> tuple:
    conn = sqlite3.connect("quant/data/market.db")
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM daily WHERE date=? ORDER BY symbol LIMIT ?",
        (date, n)).fetchall()]
    rows = conn.execute(
        "SELECT symbol, close FROM daily WHERE date=? AND symbol IN (%s)"
        % ",".join("?" * len(syms)), (date, *syms)).fetchall()
    conn.close()
    data = pd.DataFrame.from_records(rows, columns=["symbol", "close"]).set_index("symbol").T
    data.columns = pd.MultiIndex.from_product([["close"], data.columns])
    return data, syms


# ── 1. 源码断言: key 用 row["symbol"] 而非 iterrows 行号 ──
def test_symbol_key_source():
    src = open("quant/factor/compute/price/_event.py", encoding="utf-8").read()
    fn = src.split("def compute_fund_change")[1].split("def compute_analyst_buy")[0]
    assert 'scores[row["symbol"]]' in fn
    assert "for sym, row in fh.iterrows()" not in fn


# ── 2. 行为: 有 fund_hold 数据时返回非空 (修复前恒空) ──
def test_fund_change_nonempty_with_data():
    date = "2020-01-02"
    data, syms = _build(date)
    aux_full = preload_aux_data_chunk(syms, date, "2020-01-31")
    aux_day = slice_aux_for_date(aux_full, date)
    r = compute_fund_change(data, date, aux=aux_day)
    assert r is not None
    assert r.notna().sum() > len(syms) * 0.5          # 大部分股票有值
    assert (r.dropna() != 0).sum() > 0                # 非全 0 (有真实变动)


# ── 3. 行为: 值来自 fund_hold 最新报告期的 change_ratio (PIT) ──
def test_fund_change_uses_latest_period():
    date = "2020-01-22"
    data, syms = _build(date)
    aux_full = preload_aux_data_chunk(syms, "2020-01-02", "2020-01-31")
    aux_day = slice_aux_for_date(aux_full, date)
    fh = aux_day["fund_hold"]
    assert not fh.empty and fh["report_date"].nunique() == 1
    latest = fh["report_date"].iloc[0]
    r = compute_fund_change(data, date, aux=aux_day)
    conn = sqlite3.connect("quant/data/market.db")
    sym0 = r.dropna().index[0]
    expect = conn.execute(
        "SELECT change_ratio FROM fund_hold WHERE report_date=? AND symbol=?",
        (latest, sym0)).fetchone()
    conn.close()
    assert expect is not None
    got = r.dropna()[sym0]
    if expect[0] is not None:
        # zscore 保持符号: 原始值为正/负 → 标准化后同号
        assert (got > 0) == (float(expect[0]) > 0)