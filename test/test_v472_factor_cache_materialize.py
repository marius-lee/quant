"""test-v472: 因子缓存物化 v472 — 计算计划 + 向量化装配 + worker 直写 parquet part 合并。

背景 (2026-08-12 现场):
  - batch2 (23 因子 × 1846 日期) 全败 67min/0 行 — _ma_alignment 硬编码依赖 ma_5/10/20/60,
    _required_windows 按声明窗口推导漏 10 → KeyError 'ma_10' 且 shortcut 分支无 per-factor 隔离
    → 整日判失败, checkpoint 永久重试。
  - 结果装配为 Python tuple/dict 逐值 + 跨进程 pickle 全量传输 → 大 chunk 内存爆炸。

覆盖 (v472 修复方案):
  1. prims 窗口完整性 — 恒定标准窗口集, ma_alignment_20d（漏 ma_10 的硬编码依赖）物化成功
  2. 增量 append — 已物化日期跳过 (skipped), 仅新增日期物化, meta last_date 推进
  3. 失败日期续传 — 单日失败 → failed_dates → 重跑只重算失败日期 (checkpoint resume)
  4. 跨年分区 — 2 个 year parquet 分区 + load/bulk_load 往返
  5. trim 重映射 — trim_to_max_days 后 date_i16/trading_days 对齐, load 正常
  6. force 重算 — force=True 全部重算且行数 > 0
"""
import os
import sqlite3

import numpy as np
import pandas as pd
import pytest

from quant.data.repos._base import DatabaseManager
from quant.data.store import DataStore
from quant.factor.store import FactorStore

SYMS = [f"S{i:05d}" for i in range(40)]
FACTORS = ["ma_alignment_20d", "momentum_63d"]


def _wide(dates: pd.DatetimeIndex, seed: int = 0) -> pd.DataFrame:
    """生成 (dates × symbols) 宽表, MultiIndex columns (field, symbol),
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
    amount = volume * close / 1000
    turnover = rng.uniform(0.5, 5.0, (n_d, n_s))
    frames = {"open": open_, "high": high, "low": low, "close": close,
              "volume": volume, "amount": amount, "turnover": turnover}
    return pd.concat(
        {k: pd.DataFrame(v, index=dates, columns=SYMS) for k, v in frames.items()},
        axis=1)


def _seed_market_db(store: DataStore, dates: pd.DatetimeIndex, data: pd.DataFrame):
    """向测试 market.db 写入 stocks 元数据 + daily 行情。

    用独立连接写库 — 不能走 store._connect(): 关闭该连接会使
    DataStore 的 thread-local 连接保持 closed 状态, 后续
    _build_fundamentals_panel 复用时报 "Cannot operate on a closed database"。
    """
    conn = sqlite3.connect(store.db_path)
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(stocks)")}
    for col in ["pe", "pe_ttm", "pb", "total_mv", "roe", "eps", "bvps"]:
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
    conn.close()


@pytest.fixture
def materialize_env(tmp_path, monkeypatch):
    """tmp market.db + factor_cache, DatabaseManager.market -> 同一 tmp db 文件。

    aux 表 (margin_detail/analyst...) 不存在 → preload chunk 空表降级 (生产同构)。
    DataStore db_path 与 DatabaseManager.market 指向同一文件, 符号校验/辅助查询同源。
    """
    dates = pd.bdate_range("2024-09-19", periods=80)
    data = _wide(dates, seed=21)
    ds = DataStore(db_path=str(tmp_path / "market.db"))
    _seed_market_db(ds, dates, data)

    db_path = str(tmp_path / "market.db")
    monkeypatch.setattr(DatabaseManager, "market",
                        staticmethod(lambda: sqlite3.connect(db_path)))
    def _no_duckdb(*a, **k):
        raise RuntimeError("duckdb disabled in tests")
    monkeypatch.setattr("quant.data.store.get_duckdb_proxy", _no_duckdb)

    fs = FactorStore(db_path=str(tmp_path / "factor_cache.db"))
    mat_dates = [str(d)[:10] for d in dates[-10:]]
    env = {
        "ds": ds, "fs": fs,
        "dates": mat_dates,
        "date_range": mat_dates,
        "syms": SYMS,
    }
    try:
        yield env
    finally:
        try:
            fs.close()
            ds.close()
        except Exception:
            pass


def test_v472_prims_completeness_hardcoded_windows(materialize_env):
    """1. prims 窗口完整性: ma_alignment_20d 单独物化 (声明窗口仅 20, 硬编码依赖
    ma_5/10/20/60) — 修复前 KeyError 'ma_10' → 整日失败 (2026-08-12 batch2 现场)。"""
    env = materialize_env
    res = env["fs"].materialize(env["dates"][:3], ["ma_alignment_20d"],
                                env["syms"], store=env["ds"])
    assert res["failed_dates"] == [], f"修复前 KeyError ma_10 使整批失败: {res['failed_dates']}"
    assert res["n_rows"] > 0
    data = env["fs"].load(env["dates"][2], factor_names=["ma_alignment_20d"])
    assert "ma_alignment_20d" in data
    assert data["ma_alignment_20d"].notna().sum() >= 10


def test_v472_incremental_append_only(materialize_env):
    """2. 增量 append: 已物化日期跳过; 新增日期只算新值; meta last_date 推进。"""
    env = materialize_env
    fs, ds = env["fs"], env["ds"]
    d1, d2 = env["dates"][:2], env["dates"]

    r1 = fs.materialize(d1, FACTORS, env["syms"], store=ds)
    assert r1["failed_dates"] == []
    assert r1["n_rows"] > 0
    assert r1["n_dates"] == len(d1)

    r2 = fs.materialize(d1, FACTORS, env["syms"], store=ds)
    assert r2["skipped"] is True
    assert r2["n_rows"] == 0

    r3 = fs.materialize(d2, FACTORS, env["syms"], store=ds)
    assert r3["n_rows"] > 0
    assert r3["n_dates"] == len(d2) - len(d1), "只物化新增日期"

    meta = fs._load_factor_meta("momentum_63d")
    assert set(meta["dates"]) == set(d2)
    assert meta["last_date"] == d2[-1]


def test_v472_failed_date_resume(materialize_env, monkeypatch):
    """3. 失败日期续传: 单日失败 → failed_dates 上报 → 恢复后重跑只算失败日。"""
    import quant.factor.store as store_mod
    env = materialize_env
    fs, ds = env["fs"], env["ds"]
    victim = env["dates"][3]
    _orig = store_mod.compute_all_factors

    def _flaky(data, date, primitives=None, fundamentals=None, benchmark_ret=None,
               factor_names=None, status_filter=None, preloaded_financials=None,
               preloaded_fundamentals=None, preloaded_aux_chunk=None,
               factor_fail_fast=True, quiet=False, use_shortcut=True,
               financials_cache=None):
        if date == victim:
            raise RuntimeError("simulated worker failure")
        return _orig(data, date, primitives=primitives, fundamentals=fundamentals,
                     benchmark_ret=benchmark_ret, factor_names=factor_names,
                     status_filter=status_filter,
                     preloaded_financials=preloaded_financials,
                     preloaded_fundamentals=preloaded_fundamentals,
                     preloaded_aux_chunk=preloaded_aux_chunk,
                     factor_fail_fast=factor_fail_fast, quiet=quiet,
                     use_shortcut=use_shortcut, financials_cache=financials_cache)

    monkeypatch.setattr(store_mod, "compute_all_factors", _flaky)
    r1 = fs.materialize(env["dates"], FACTORS, env["syms"], store=ds)
    assert r1["failed_dates"] == [victim]
    assert victim not in fs._load_factor_meta("momentum_63d")["dates"]

    monkeypatch.setattr(store_mod, "compute_all_factors", _orig)
    r2 = fs.materialize(env["dates"], FACTORS, env["syms"], store=ds)
    assert r2["failed_dates"] == [], "恢复后失败日期应补算成功"
    assert victim in fs._load_factor_meta("momentum_63d")["dates"]
    data = fs.load(victim, factor_names=["momentum_63d"])
    assert data["momentum_63d"].notna().sum() >= 10


def test_v472_cross_year_partition_roundtrip(materialize_env):
    """4. 跨年分区: 10 日期跨 2024/2025 → 两个 year 分区, load/bulk_load 往返一致。"""
    env = materialize_env
    fs, ds = env["fs"], env["ds"]
    years = sorted({d[:4] for d in env["dates"]})
    assert years == ["2024", "2025"], "fixture 应跨年"

    r = fs.materialize(env["dates"], FACTORS, env["syms"], store=ds)
    assert r["failed_dates"] == []

    for year in years:
        for f in FACTORS:
            assert (env["ds"] and fs._parquet_path(f, int(year)) and
                    __import__("os").path.exists(fs._parquet_path(f, int(year)))), \
                f"{f}/{year}.parquet 缺失"

    for d in env["dates"]:
        fv = fs.load(d, factor_names=FACTORS)
        assert set(fv) == set(FACTORS), f"{d}: {set(fv)} != {set(FACTORS)}"

    bulk = fs.bulk_load(env["dates"], factor_names=FACTORS)
    assert set(bulk) == set(env["dates"])
    for d, fv in bulk.items():
        assert fv["momentum_63d"].notna().sum() >= 10


def test_v472_trim_remaps_date_index(materialize_env):
    """5. trim_to_max_days: date_i16 重映射 + trading_days/meta 裁剪, load 对齐。"""
    env = materialize_env
    fs, ds = env["fs"], env["ds"]
    r = fs.materialize(env["dates"], FACTORS, env["syms"], store=ds)
    assert r["failed_dates"] == []

    deleted = fs.trim_to_max_days(4)
    assert deleted > 0
    kept_dates = fs.list_cached_dates()
    assert kept_dates == env["dates"][-4:]

    fv = fs.load(env["dates"][-1], factor_names=FACTORS)
    assert set(fv) == set(FACTORS)
    assert fs.is_materialized([env["dates"][-1]], FACTORS)
    assert not fs.is_materialized([env["dates"][0]], FACTORS), "裁剪后旧日期应缺失"


def test_v472_force_recompute(materialize_env):
    """6. force=True: 全量重算, 行数与首跑一致。"""
    env = materialize_env
    fs, ds = env["fs"], env["ds"]
    r1 = fs.materialize(env["dates"], FACTORS, env["syms"], store=ds)
    r2 = fs.materialize(env["dates"], FACTORS, env["syms"], store=ds, force=True)
    assert r2["failed_dates"] == []
    assert r2["n_rows"] == r1["n_rows"]
    assert r2["n_dates"] == len(env["dates"])


def _part_files(fs) -> list[str]:
    found = []
    for fname in sorted(os.listdir(fs._parquet_dir)):
        fdir = os.path.join(fs._parquet_dir, fname)
        if not os.path.isdir(fdir):
            continue
        for f in sorted(os.listdir(fdir)):
            if ".part" in f:
                found.append(os.path.join(fname, f))
    return found


def test_v477_part_merge_no_residue(materialize_env):
    """part 合并: 物化结束后无 .part 残留, 主 parquet 完整, load 正常。"""
    env = materialize_env
    fs, ds = env["fs"], env["ds"]
    r = fs.materialize(env["dates"], FACTORS, env["syms"], store=ds)
    assert r["failed_dates"] == []
    assert _part_files(fs) == [], f"part 残留: {_part_files(fs)}"
    for year in sorted({d[:4] for d in env["dates"]}):
        for f in FACTORS:
            ppath = fs._parquet_path(f, int(year))
            assert os.path.exists(ppath), f"{f}/{year}.parquet 缺失"
    for d in env["dates"]:
        fv = fs.load(d, factor_names=FACTORS)
        assert set(fv) == set(FACTORS), f"{d}: {set(fv)}"

    # 增量再物化应复用主文件, 无 part 残留
    r2 = fs.materialize([env["dates"][0]], FACTORS, env["syms"], store=ds)
    assert r2["skipped"] is True
    assert _part_files(fs) == []


def test_v477_part_merge_reentrant_stale_parts(materialize_env):
    """part 合并可重入: 模拟中断残留 part → 再次 materialize 开头幂等合并。"""
    env = materialize_env
    fs, ds = env["fs"], env["ds"]
    # 正常物化一段
    r1 = fs.materialize(env["dates"][:4], FACTORS, env["syms"], store=ds)
    assert r1["failed_dates"] == []
    # 手工制造残留 part (覆盖已有日期一行)
    import pandas as pd
    import numpy as np
    stale = pd.DataFrame({
        "date_i16": np.int16([0]),
        "symbol_i16": np.int16([0]),
        "value_f32": np.float32([123.0]),
    })
    for f in FACTORS:
        os.makedirs(os.path.join(fs._parquet_dir, f), exist_ok=True)
        stale.to_parquet(fs._part_path(f, int(env["dates"][0][:4]), 999),
                         compression="zstd", compression_level=3, index=False)
    assert len(_part_files(fs)) == len(FACTORS)

    # 再次物化 (全量含已物化日期 + 残留 part)
    r2 = fs.materialize(env["dates"][:4], FACTORS, env["syms"], store=ds)
    assert r2["failed_dates"] == []
    assert _part_files(fs) == [], f"part 残留未清理: {_part_files(fs)}"
    # 残留 part 的 (date0, sym0) 行应被合并进主文件 (keep=last 语义)
    fv = fs.load(env["dates"][0], factor_names=["momentum_63d"])
    assert "momentum_63d" in fv