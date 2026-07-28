"""test-v303: tickflow 注册版→免费层权限感知故障转移.

背景: 注册 key 套餐无批量K线权限 (PermissionError 实测),
历史K须落免费层; 进程内 flag 记住, 不重复白试。

test-v304 追加: tickflow 日K 未复权 → 本地 adj_factor 转 qfq (同 tushare 口径)。
"""
import sqlite3
import sys
import types

import pandas as pd
import pytest

from quant.data import store as store_mod


def _fake_tickflow_module(calls):
    m = types.ModuleType("tickflow")

    class PermissionError(Exception):
        pass

    class _FakeKlines:
        def __init__(self, behavior, tag):
            self._behavior, self._tag = behavior, tag

        def batch(self, codes, **kw):
            calls.append(self._tag)
            if self._behavior == "perm":
                raise PermissionError("无日/周/月K线查询批量查询权限")
            return {"000001.SZ": pd.DataFrame()}

    class TickFlow:
        def __init__(self, api_key=None):
            self.klines = _FakeKlines("perm", "authed")

        @classmethod
        def free(cls):
            inst = cls.__new__(cls)
            inst.klines = _FakeKlines("ok", "free")
            return inst

    m.PermissionError = PermissionError
    m.TickFlow = TickFlow
    return m


@pytest.fixture
def fake_tf(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "tickflow", _fake_tickflow_module(calls))
    store_mod._TICKFLOW_BATCH_NO_PERM = False
    yield calls
    store_mod._TICKFLOW_BATCH_NO_PERM = False


def _bare_store(monkeypatch):
    """内存 DB 的 DataStore (含 adj_factor 表) + 屏蔽当日 quotes 补充."""
    ds = store_mod.DataStore.__new__(store_mod.DataStore)
    monkeypatch.setattr(ds, "_fetch_tickflow_quotes", lambda symbols, date: [])
    conn = sqlite3.connect(":memory:")
    ds._ensure_adj_factor_tables(conn)
    monkeypatch.setattr(ds, "_connect", lambda: conn)
    return ds


def _seed_factors(ds, factors):
    """factors: [(symbol6, 'YYYY-MM-DD', factor), ...] 写入内存 adj_factor 表."""
    conn = ds._connect()
    conn.executemany(
        "INSERT OR REPLACE INTO adj_factor(symbol, date, factor) VALUES(?,?,?)",
        factors)
    conn.commit()


class TestTickflowFailover:
    def test_permission_error_falls_back_to_free(self, fake_tf, monkeypatch):
        ds = _bare_store(monkeypatch)
        ds._fetch_tickflow_daily(["000001"])
        assert fake_tf == ["authed", "free"]
        assert store_mod._TICKFLOW_BATCH_NO_PERM is True

    def test_flag_set_skips_authed_next_time(self, fake_tf, monkeypatch):
        store_mod._TICKFLOW_BATCH_NO_PERM = True
        ds = _bare_store(monkeypatch)
        ds._fetch_tickflow_daily(["000001"])
        assert fake_tf == ["free"]

    def test_flag_reset_allows_retry(self, fake_tf, monkeypatch):
        """flag 复位 (新进程/升级套餐) → 重新先试注册版."""
        ds = _bare_store(monkeypatch)
        ds._fetch_tickflow_daily(["000001"])
        assert store_mod._TICKFLOW_BATCH_NO_PERM is True
        store_mod._TICKFLOW_BATCH_NO_PERM = False  # 模拟新进程
        ds._fetch_tickflow_daily(["000001"])
        assert fake_tf == ["authed", "free", "authed", "free"]


# ── test-v304: tickflow 未复权日K → 本地 adj_factor 转 qfq ──


def _klines_df(dates, price=100.0):
    n = len(dates)
    return pd.DataFrame({
        "trade_date": dates,
        "open": [price] * n, "high": [price] * n,
        "low": [price] * n, "close": [price] * n,
        "volume": [1000] * n, "amount": [100000.0] * n,
    })


def _fake_tf_with_data(dfs_map):
    """返回真实 K 线 df 的 fake tickflow (注册版直接成功)."""
    m = types.ModuleType("tickflow")

    class PermissionError(Exception):
        pass

    class _FakeKlines:
        def batch(self, codes, **kw):
            return {c: dfs_map[c].copy() for c in codes if c in dfs_map}

    class TickFlow:
        def __init__(self, api_key=None):
            self.klines = _FakeKlines()

        @classmethod
        def free(cls):
            return cls()

    m.PermissionError = PermissionError
    m.TickFlow = TickFlow
    return m


class TestTickflowQfq:
    def test_pre_exdiv_prices_scaled_by_factor_ratio(self, monkeypatch):
        """10送10 除权 (因子 1.0→2.0, latest=2.0): 除权前 100 → qfq 50."""
        dfs = {"000001.SZ": _klines_df(["2026-07-17", "2026-07-20", "2026-07-21"])}
        monkeypatch.setitem(sys.modules, "tickflow", _fake_tf_with_data(dfs))
        ds = _bare_store(monkeypatch)
        _seed_factors(ds, [
            ("000001", "2026-07-17", 1.0),
            ("000001", "2026-07-20", 2.0),
            ("000001", "2026-07-21", 2.0),
        ])
        rows = ds._fetch_tickflow_daily(["000001"], "2026-07-15")
        by_date = {r[1]: r for r in rows}
        assert by_date["2026-07-17"][5] == 50.0    # close 100 × 1.0/2.0
        assert by_date["2026-07-17"][2] == 50.0    # open 同比例
        assert by_date["2026-07-20"][5] == 100.0
        assert by_date["2026-07-21"][5] == 100.0

    def test_no_coverage_returns_none_for_next_source(self, monkeypatch):
        """全批无本地因子 → None 交下一源 (不写口径不一致数据)."""
        dfs = {"000001.SZ": _klines_df(["2026-07-21"])}
        monkeypatch.setitem(sys.modules, "tickflow", _fake_tf_with_data(dfs))
        ds = _bare_store(monkeypatch)
        assert ds._fetch_tickflow_daily(["000001"], "2026-07-15") is None

    def test_uncovered_symbol_skipped_covered_kept(self, monkeypatch):
        """部分覆盖: 无因子股票跳过, 有因子股票照常返回."""
        dfs = {
            "000001.SZ": _klines_df(["2026-07-21"]),
            "600519.SH": _klines_df(["2026-07-21"]),
        }
        monkeypatch.setitem(sys.modules, "tickflow", _fake_tf_with_data(dfs))
        ds = _bare_store(monkeypatch)
        _seed_factors(ds, [("600519", "2026-07-21", 1.5)])
        rows = ds._fetch_tickflow_daily(["000001", "600519"], "2026-07-15")
        assert {r[0] for r in rows} == {"600519"}

    def test_compact_yyyymmdd_dates_match_factor_keys(self, monkeypatch):
        """tickflow 返回 YYYYMMDD 紧凑日期 → 归一化后照常匹配因子键."""
        dfs = {"000001.SZ": _klines_df(["20260717", "20260720"])}
        monkeypatch.setitem(sys.modules, "tickflow", _fake_tf_with_data(dfs))
        ds = _bare_store(monkeypatch)
        _seed_factors(ds, [
            ("000001", "2026-07-17", 1.0),
            ("000001", "2026-07-20", 2.0),
        ])
        rows = ds._fetch_tickflow_daily(["000001"], "2026-07-15")
        by_date = {r[1]: r for r in rows}  # _norm_row → to_str 归一化 ISO
        assert by_date["2026-07-17"][5] == 50.0
        assert by_date["2026-07-20"][5] == 100.0

    def test_suspension_day_factor_ffilled(self, monkeypatch):
        """停牌日后复牌: K线日期与因子日不完全重叠 → ffill/bfill 取邻近因子."""
        # 因子只有 07-17(1.0)/07-21(2.0); K线 07-20 无因子记录 → ffill 得 1.0
        dfs = {"000001.SZ": _klines_df(["2026-07-17", "2026-07-20", "2026-07-21"])}
        monkeypatch.setitem(sys.modules, "tickflow", _fake_tf_with_data(dfs))
        ds = _bare_store(monkeypatch)
        _seed_factors(ds, [
            ("000001", "2026-07-17", 1.0),
            ("000001", "2026-07-21", 2.0),
        ])
        rows = ds._fetch_tickflow_daily(["000001"], "2026-07-15")
        by_date = {r[1]: r for r in rows}
        assert by_date["2026-07-17"][5] == 50.0
        assert by_date["2026-07-20"][5] == 50.0   # ffill 1.0 → ratio 0.5
        assert by_date["2026-07-21"][5] == 100.0
