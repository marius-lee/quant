# v535 tests — Kelly cov 链路 / XGB 早停集分离 / 全市场 rank / LW π̂ pairwise /
# sync_delisted_stocks 中文列名 / phase7 PIT 因子池 / 年化口径统一.
import sqlite3

import pandas as pd
import pytest

from quant.data.repos.factor_repo import FactorRepo
from quant.factor.compute import _registry, get_factor_names


# ── 1. sync_delisted_stocks 中文列名修复 (审计项5: 幸存者偏差) ──
def test_sync_delisted_chinese_columns(monkeypatch, tmp_path):
    """akshare 退市接口返回中文列名 → 原代码全部写成 symbol=000000.

    回归: SH 取 公司代码/暂停上市日期, SZ 取 证券代码/终止上市日期,
    且 concat 同名列不得使 row.get 返回 Series.
    """
    import quant.data.store as S

    monkeypatch.setattr(S, "MARKET_DB", str(tmp_path / "mkt.db"))
    sh = pd.DataFrame({
        "公司代码": ["600001", "600002"],
        "公司简称": ["邯郸钢铁", "齐鲁退市"],
        "上市日期": ["1998-01-22", "1998-04-08"],
        "暂停上市日期": ["2009-12-29", "2006-04-24"],
        "证券代码": [None, None],
        "证券简称": [None, None],
        "终止上市日期": [None, None],
    })
    sz = pd.DataFrame({
        "公司代码": [None],
        "公司简称": [None],
        "上市日期": ["2000-03-15"],
        "暂停上市日期": [None],
        "证券代码": ["000030"],
        "证券简称": ["莱茵达"],
        "终止上市日期": ["2019-11-28"],
    })

    def fake_a_delist():
        raise AttributeError("no stock_info_a_delist")

    import akshare as ak
    monkeypatch.setattr(ak, "stock_info_a_delist", fake_a_delist, raising=False)  # akshare lazy __getattr__
    monkeypatch.setattr(ak, "stock_info_sh_delist", lambda: sh, raising=False)
    monkeypatch.setattr(ak, "stock_info_sz_delist", lambda: sz, raising=False)

    n = S.DataStore(db_path=str(tmp_path / "mkt.db")).sync_delisted_stocks()
    assert n == 3

    c = sqlite3.connect(str(tmp_path / "mkt.db"))
    rows = c.execute(
        "SELECT symbol, name, list_status, delist_date FROM stocks"
    ).fetchall()
    c.close()
    rows = sorted(rows)
    assert rows[0] == ("000030", "莱茵达", "D", "2019-11-28")
    assert rows[1] == ("600001", "邯郸钢铁", "D", "2009-12-29")
    assert rows[2] == ("600002", "齐鲁退市", "D", "2006-04-24")
    assert all(r[0] != "000000" for r in rows)


def test_sync_delisted_idempotent(monkeypatch, tmp_path):
    """同一份退市名单重复 sync 不重复入库 (INSERT OR IGNORE / UPDATE)."""
    import quant.data.store as S

    monkeypatch.setattr(S, "MARKET_DB", str(tmp_path / "mkt.db"))
    sh = pd.DataFrame({
        "公司代码": ["600001"],
        "公司简称": ["邯郸钢铁"],
        "上市日期": ["1998-01-22"],
        "暂停上市日期": ["2009-12-29"],
        "证券代码": [None],
        "证券简称": [None],
        "终止上市日期": [None],
    })

    def fake_a_delist():
        raise AttributeError("no stock_info_a_delist")

    import akshare as ak
    monkeypatch.setattr(ak, "stock_info_a_delist", fake_a_delist, raising=False)  # akshare lazy __getattr__
    monkeypatch.setattr(ak, "stock_info_sh_delist", lambda: sh, raising=False)
    monkeypatch.setattr(ak, "stock_info_sz_delist",
                        lambda: sh.iloc[0:0].copy())

    store = S.DataStore(db_path=str(tmp_path / "mkt.db"))
    assert store.sync_delisted_stocks() == 1
    assert store.sync_delisted_stocks() == 0

    c = sqlite3.connect(str(tmp_path / "mkt.db"))
    n = c.execute("SELECT COUNT(*) FROM stocks WHERE list_status='D'").fetchone()[0]
    c.close()
    assert n == 1


# ── 2. phase7 PIT 因子池 (审计项6): registered_before SQL 过滤 ──
def test_factor_repo_registered_before(monkeypatch):
    captured = {}

    def fake_query(self, sql, params=()):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(FactorRepo, "_query", fake_query)
    FactorRepo().get_factors_by_status(("active",), ["mom_20"],
                                       registered_before="2020-01-01")
    assert "created_at <= datetime(?)" in captured["sql"]
    assert captured["params"][-1] == "2020-01-01"


def test_factor_repo_registered_before_none(monkeypatch):
    captured = {}

    def fake_query(self, sql, params=()):
        captured["sql"] = sql
        return []

    monkeypatch.setattr(FactorRepo, "_query", fake_query)
    FactorRepo().get_factors_by_status(("active",), ["mom_20"])
    assert "created_at" not in captured["sql"]


def test_get_factor_names_registered_before(monkeypatch):
    seen = {}

    def fake_price(status, registered_before=None):
        seen["price"] = (status, registered_before)
        return {}

    def fake_fund(status, registered_before=None):
        seen["fund"] = (status, registered_before)
        return {}

    monkeypatch.setattr(_registry, "load_active_price_factors", fake_price)
    monkeypatch.setattr(_registry, "load_active_fundamental_factors", fake_fund)
    get_factor_names(status_filter="backtesting", registered_before="2021-06-30")
    assert seen["price"] == ("backtesting", "2021-06-30")
    assert seen["fund"] == ("backtesting", "2021-06-30")


# ── 3. build_forward_returns PIT asof universe (审计项5) ──
def test_build_forward_returns_pit_start(monkeypatch):
    from quant.alpha import qlib_model as Q

    captured = {}

    class FakeStore:
        def get_daily(self, symbols, start, end):
            captured["symbols"] = symbols
            cols = pd.MultiIndex.from_product([["close"], ["600001"]])
            return pd.DataFrame(
                [[10.0], [11.0], [12.0]],
                index=pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06"]),
                columns=cols,
            )

        def close(self):
            pass

    class FakeUniverse:
        def get_symbols(self, exclude_market="BJ", start_date=None):
            captured["start_date"] = start_date
            return ["600001"]

    monkeypatch.setattr("quant.data.store.DataStore", lambda: FakeStore())
    monkeypatch.setattr("quant.data.repos.UniverseRepo", lambda: FakeUniverse())
    out = Q.build_forward_returns(start_date="2020-01-01",
                                  end_date="2020-01-31", horizon=1)
    assert captured["start_date"] == "2020-01-01"
    assert captured["symbols"] == ["600001"]
    assert out.name == "forward_return"


# ── 4. 年化口径统一 (审计项7): 常量来源 ──
def test_tear_sheet_uses_config_annual_days(monkeypatch):
    """tear_sheet 的 Sharpe/波动年化必须读 config, 不得硬编码 244."""
    import quant.evaluation.tear_sheet as T
    from quant.config.constants import _require_cfg

    src = open(T.__file__, encoding="utf-8").read()
    assert "np.sqrt(_ann)" in src or "_require_cfg(\"market.annual_trading_days\")" in src
    assert "np.sqrt(244)" not in src
    assert float(_require_cfg("market.annual_trading_days")) == 244.0


def test_stats_cache_uses_config_annual_days():
    """stats_cache ICIR 年化不得硬编码 252."""
    import quant.factor.stats_cache as SC

    src = open(SC.__file__, encoding="utf-8").read()
    assert "np.sqrt(252" not in src