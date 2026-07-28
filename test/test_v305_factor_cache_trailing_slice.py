"""test-v305: factor_cache trailing slice fix — regression tests for the 7 window factors.

根因: store.py materialize 用 data_full.loc[[ts]] (1 行切片) 喂 compute_all_factors,
窗口因子 (get_loc(date) + iloc[start:idx+1] 自切窗口) 拿不到历史 -> 全 NaN -> 0 行
-> is_materialized 永 False -> 每次全量重算死循环。
修复: .loc[[ts]] -> .loc[:ts] (point-in-time trailing slice, 索引 <= ts 无前视)。

覆盖 (HANDOFF test-v305 修复方案第 3 条):
  1. trailing slice 语义 — 早期日期收到全部 <= ts 历史
  2. 无前视 — t 日因子值不随 t+1..t+n 数据变化
  3. 7 因子物化后真实产行 (abn_turnover, amihud_250d, ctr_20d, dt_streak,
     hl_volume_20d, ideal_amplitude, zt_streak)
"""
import sqlite3

import numpy as np
import pandas as pd
import pytest

from quant.factor.compute.price._momentum import (
    compute_amihud, compute_intraday_range,
)
from quant.factor.compute.price._turnover import compute_ctr
from quant.data.repos._base import DatabaseManager
from quant.data.store import DataStore
from quant.factor.store import FactorStore

SYMS = [f"S{i:05d}" for i in range(40)]

# 死循环中永不物化的 7 个窗口因子 (HANDOFF test-v305 根因链 2)
WINDOW7 = ["abn_turnover", "amihud_250d", "ctr_20d", "dt_streak",
           "hl_volume_20d", "ideal_amplitude", "zt_streak"]


def _wide(dates: pd.DatetimeIndex, seed: int = 0) -> pd.DataFrame:
    """生成 (dates x symbols) 宽表, MultiIndex columns (field, symbol),
    结构同 DataStore.get_daily 输出。确定性 (default_rng 固定种子)。"""
    rng = np.random.default_rng(seed)
    n_d, n_s = len(dates), len(SYMS)
    ret = rng.normal(0.0005, 0.015, (n_d, n_s))
    close = 10.0 * np.exp(np.cumsum(ret, axis=0))
    spread = np.abs(rng.normal(0.01, 0.003, (n_d, n_s)))
    high = close * (1 + spread)
    low = close * (1 - spread)
    open_ = close * (1 + rng.normal(0, 0.003, (n_d, n_s)))
    volume = rng.uniform(1e6, 5e6, (n_d, n_s))
    amount = volume * close / 1000  # 千元
    turnover = rng.uniform(0.5, 5.0, (n_d, n_s))
    frames = {"open": open_, "high": high, "low": low, "close": close,
              "volume": volume, "amount": amount, "turnover": turnover}
    return pd.concat(
        {k: pd.DataFrame(v, index=dates, columns=SYMS) for k, v in frames.items()},
        axis=1)


def test_trailing_slice_point_in_time():
    """1. trailing slice 语义: 早期日期收到全部 <= ts 历史, 无前视。"""
    idx = pd.bdate_range("2026-01-01", periods=60)
    df = pd.DataFrame(np.arange(60 * 3).reshape(60, 3), index=idx, columns=list("abc"))
    for pos in (0, 5, 59):
        ts = idx[pos]
        day = df.loc[:ts]
        assert day.index[-1] == ts            # iloc[-1] = ts 当日, 同旧 1 行切片语义
        assert (day.index <= ts).all()        # 无前视
        assert len(day) == pos + 1


def test_window_factor_no_lookahead():
    """2. 无前视: t 日因子值不随 t+1..t+n 数据变化。"""
    dates = pd.bdate_range("2025-01-01", periods=300)
    data = _wide(dates, seed=7)
    t = dates[-6]  # 之后还有 5 天 "未来" 数据
    t_str = str(t)[:10]
    v_full = compute_intraday_range(data, t_str)
    v_trail = compute_intraday_range(data.loc[:t], t_str)
    pd.testing.assert_series_equal(v_full, v_trail)


def test_window_factor_needs_history():
    """3a. bug 机制钉死: 1 行输入 -> 全 NaN; trailing slice -> 有值。"""
    dates = pd.bdate_range("2025-01-01", periods=300)
    data = _wide(dates, seed=3)
    t = dates[-1]
    t_str = str(t)[:10]

    v_full = compute_amihud(data, t_str)
    assert v_full.notna().sum() >= 30  # zscore_min_count_dense=30, 全 universe 有值

    # 修复前的 1 行切片 — 窗口因子拿不到历史, 全 NaN (死循环根因)
    v_1row = compute_amihud(data.loc[[t]], t_str)
    assert v_1row.isna().all()


def test_ctr_inf_turnover_no_cross_section_poison():
    """3c. ctr_20d inf 防腐: turnover 零填充段 0→x 跳变产生 inf,
    修复前单只 inf 使截面 zscore std=NaN → 全 universe 0 行 (实证根因)。"""
    dates = pd.bdate_range("2026-06-01", periods=25)
    data = _wide(dates, seed=5)
    t_str = str(dates[-1])[:10]

    # S00000: 前 20 天 turnover=0 (零填充段), 后 5 天正常 → 0→x 跳变 inf
    data.loc[dates[:20], ("turnover", "S00000")] = 0.0

    r = compute_ctr(data, t_str, 20)
    assert r.notna().sum() >= 30, \
        f"inf 污染截面: 仅 {r.notna().sum()} 只有值 (修复前为 0)"
    # 零填充股票自身可被丢弃, 但不得拖垮其他股票
    assert np.isfinite(r.dropna()).all()


def _seed_market_db(store: DataStore, dates: pd.DatetimeIndex, data: pd.DataFrame):
    """向测试 market.db 写入 stocks 元数据 + daily 行情 (get_daily 读取源)。"""
    conn = store._connect()
    # get_fundamentals 需要 stocks 的快照列 (pe/pb/total_mv/...)
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(stocks)")}
    for col in ["pe", "pe_ttm", "pb", "total_mv", "roe", "high_52w", "eps", "bvps"]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE stocks ADD COLUMN {col} REAL")
    conn.executemany(
        "INSERT OR REPLACE INTO stocks(symbol, name, market, list_date, industry) "
        "VALUES (?, ?, ?, ?, ?)",
        [(s, f"MOCK{i}", "SZ" if i % 2 else "SH", "2020-01-01", "TEST")
         for i, s in enumerate(SYMS)])
    rows = []
    for d in dates:
        ds = str(d)[:10]
        for s in SYMS:
            rows.append((s, ds,
                         float(data["open"].loc[d, s]), float(data["high"].loc[d, s]),
                         float(data["low"].loc[d, s]), float(data["close"].loc[d, s]),
                         float(data["volume"].loc[d, s]), float(data["amount"].loc[d, s]),
                         float(data["turnover"].loc[d, s])))
    conn.executemany(
        "INSERT OR REPLACE INTO daily(symbol, date, open, high, low, close, "
        "volume, amount, turnover) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()


@pytest.fixture
def stub_market_conn(monkeypatch):
    """DatabaseManager.market() -> 内存 sqlite (每次新建连接)。

    覆盖两条独立 DB 通路 (均不经 DataStore 注入):
      - preload_aux_data (aux["stocks"], zt/dt_streak 板块阈值必需)
      - abn_turnover 的 stocks meta 查询 (total_mv NULL -> common<30 ->
        早退路径, 跳过 sklearn OLS, 仍产 zscore(-ln_turnover) 行)
    daily 空表供 preload_ztd_cache 查询 (no-rows 警告路径, 不抛错)。

    注意: 每次 DatabaseManager.market() 调用返回新连接 (生产语义)。
    调用方自行 close(), 互不干扰。"""
    # Use a factory that returns a fresh in-memory copy each time
    def _make_stub():
        stub = sqlite3.connect(":memory:")
        stub.execute("CREATE TABLE stocks (symbol TEXT PRIMARY KEY, name TEXT, "
                     "market TEXT, list_date TEXT, industry TEXT, total_mv REAL)")
        stub.executemany(
            "INSERT INTO stocks(symbol, name, market, list_date, industry, total_mv) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            [(s, f"MOCK{i}", "SZ" if i % 2 else "SH", "2020-01-01", None)
             for i, s in enumerate(SYMS)])
        stub.execute("CREATE TABLE daily (symbol TEXT, date TEXT, volume REAL)")
        stub.commit()
        return stub

    # Track stubs so we can close them all at test end
    _stubs = []
    def _market_factory():
        s = _make_stub()
        _stubs.append(s)
        return s

    monkeypatch.setattr(DatabaseManager, "market", staticmethod(_market_factory))
    yield
    for s in _stubs:
        try:
            s.close()
        except Exception:
            pass


def test_materialize_window_factors_produce_rows(tmp_path, stub_market_conn):
    """3b. 集成回归: materialize 后 7 窗口因子全部产行 (修复前 0 行死循环)。"""
    dates = pd.bdate_range("2025-06-02", periods=300)
    data = _wide(dates, seed=11)

    # 构造目标日涨/跌停 (主板非 ST, 阈值 ±9.5%):
    # zt/dt_streak 需截面 std>0 — 全零日 zscore -> NaN -> 永不齐 (HANDOFF 残留陷阱)
    target = dates[-1]
    prev = dates[-2]
    for s in ("S00038", "S00039"):  # 涨停: close == high, ret = +10%
        data.loc[target, ("open", s)] = data.loc[prev, ("close", s)]
        data.loc[target, ("close", s)] = data.loc[prev, ("close", s)] * 1.10
        data.loc[target, ("high", s)] = data.loc[target, ("close", s)]
        data.loc[target, ("low", s)] = data.loc[prev, ("close", s)]
    for s in ("S00036", "S00037"):  # 跌停: close == low, ret = -10%
        data.loc[target, ("open", s)] = data.loc[prev, ("close", s)]
        data.loc[target, ("close", s)] = data.loc[prev, ("close", s)] * 0.90
        data.loc[target, ("low", s)] = data.loc[target, ("close", s)]
        data.loc[target, ("high", s)] = data.loc[prev, ("close", s)]

    ds = DataStore(db_path=str(tmp_path / "market.db"))
    _seed_market_db(ds, dates, data)

    fs = FactorStore(db_path=str(tmp_path / "factor_cache.db"))
    t_str = str(target)[:10]
    res = fs.materialize([t_str], WINDOW7, SYMS, store=ds)

    assert res["n_rows"] > 0, "修复前实跑症状: materialized ... -> 0 rows"
    existing = fs._get_existing_factors(t_str)
    missing = set(WINDOW7) - existing
    assert not missing, f"修复前这 7 因子永不物化 (死循环); 仍缺: {missing}"
    assert fs.is_materialized([t_str], WINDOW7)

    # 行数核对: 每因子 >= zscore_min_count_dense(30) (ADR-039: gzip CSV backend)
    data = fs.load(t_str, factor_names=WINDOW7)
    for f in WINDOW7:
        assert f in data, f"{f}: missing from load() result"
        n = data[f].notna().sum()
        assert n >= 30, f"{f}: {n} rows < 30"
    fs.close()
    ds.close()
