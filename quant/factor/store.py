"""因子值物化存储 — 因子回测与策略回测共享的数据底座。

设计原则:
  - 因子值物化一次, 因子回测和策略回测多次消费
  - 独立于 market.db (因子值是可重算的衍生数据)
  - 调用方通过 load() 获取因子值, 不关心底层存储是 DB 还是内存

对标: VN.PY 信号表(parquet) + DolphinDB 因子数据库
"""

import sqlite3
import os
import pandas as pd
import numpy as np
from quant.utils.logger import get_logger
from quant.factor.compute.price._alternative import preload_ztd_cache

_log = get_logger("factor.store")

_DB_NAME = "factor_cache.db"
# 项目根目录 (quant/factor/store.py → quant/factor → quant → project_root)
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_PATH = os.path.join(_PROJ_ROOT, "quant", "data", _DB_NAME)

# 建表 SQL
_DDL = """
CREATE TABLE IF NOT EXISTS factor_values (
    date       TEXT    NOT NULL,
    symbol     TEXT    NOT NULL,
    factor     TEXT    NOT NULL,
    raw_value  REAL,
    zscore     REAL,
    PRIMARY KEY (date, symbol, factor)
);

CREATE INDEX IF NOT EXISTS idx_fv_date_factor ON factor_values(date, factor);
CREATE INDEX IF NOT EXISTS idx_fv_date ON factor_values(date);

CREATE TABLE IF NOT EXISTS materialization_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    date_start  TEXT NOT NULL,
    date_end    TEXT NOT NULL,
    n_factors   INTEGER,
    n_symbols   INTEGER,
    n_dates     INTEGER,
    n_rows      INTEGER,
    elapsed_sec REAL,
    force       INTEGER DEFAULT 0
);
"""


class FactorStore:
    """因子值物化存储。

    使用流程:
      1. store.materialize(date_range, factor_names) → 批量计算并存入 DB
      2. store.load(date, symbols, factor_names) → {factor_name: Series(symbol→value)}
      3. store.is_materialized(date_range, factor_names) → bool
    """

    def __init__(self, db_path: str = _DEFAULT_PATH):
        self._db = db_path
        self._conn: sqlite3.Connection | None = None
        self._ensure_tables()

    # ── DB 管理 ──

    def _ensure_tables(self):
        os.makedirs(os.path.dirname(self._db), exist_ok=True)
        c = sqlite3.connect(self._db)
        c.executescript(_DDL)
        c.commit()
        c.close()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── 物化 ──

    def materialize(self,
                    date_range: list[str],
                    factor_names: list[str],
                    symbols: list[str],
                    store: "DataStore" = None,
                    force: bool = False) -> dict:
        """批量物化因子值: 预加载数据 + 原语, 逐日计算全部因子, 批量写入 DB。

        Args:
            date_range: 交易日列表 ["2026-01-02", "2026-01-03", ...]
            factor_names: 因子名列表
            symbols: 股票池
            store: DataStore 实例 (用于加载行情数据)
            force: True 时删除旧数据重新物化

        Returns:
            dict: {n_dates, n_factors, n_symbols, n_rows, elapsed_sec}
        """
        import time as _time
        from quant.factor.compute._dispatch import compute_all_factors
        from quant.factor.compute._primitives import precompute_primitives
        from quant.data.store import DataStore

        if store is None:
            store = DataStore()

        t0 = _time.time()

        # 0. 检查已有覆盖
        if not force and self.is_materialized(date_range, factor_names):
            _log.info("factor_cache: all dates already materialized, skip")
            return {"n_dates": len(date_range), "n_factors": len(factor_names),
                    "n_symbols": len(symbols), "n_rows": 0, "elapsed_sec": 0,
                    "skipped": True}

        # 0.5 清理孤儿因子数据: 因子状态变更 (monitoring→retired→rejected) 后,
        # 增量路径 (force=False) 需主动删除已排除因子的旧数据, 避免垃圾累积。
        # 设计原则: 因子值是可重算的衍生数据, 丢失后可用 force=True 重建。
        if not force:
            c = self._get_conn()
            stored = set(r[0] for r in c.execute("SELECT DISTINCT factor FROM factor_values").fetchall())
            factor_set = set(factor_names)
            orphans = stored - factor_set
            if orphans:
                c.execute(
                    f"DELETE FROM factor_values WHERE factor IN ({','.join('?' * len(orphans))})",
                    list(orphans))
                c.commit()
                _log.info("factor_cache: pruned %d orphan factors: %s", len(orphans), sorted(orphans))

        # 1. 预加载全部行情数据
        start_dt = date_range[0]
        end_dt = date_range[-1]
        from quant.factor.windows import max_factor_calendar_days
        from quant.config.constants import _require_cfg
        _eff_days = max(_require_cfg("data.lookback_days"), max_factor_calendar_days(None))
        full_start = (pd.Timestamp(start_dt) - pd.Timedelta(days=_eff_days)).strftime("%Y-%m-%d")
        data_full = store.get_daily(symbols, start=full_start, end=end_dt)
        _log.info("factor_cache: loaded %d days × %d symbols data", len(data_full), len(symbols))

        # 2. 预计算共享原语
        prims = precompute_primitives(data_full)
        _log.info("factor_cache: primitives ready (%d tables)", len(prims))

       # 2.25 加载沪深300基准收益 (residual_momentum_126d / idio_vol_126d 共用)
       # 来源: AQR (2014) — 残差动量; Ang et al. (2006) — 特质波动; 均需CAPM基准
        # 数据从 benchmark_daily 表获取, 非 daily 表 (指数不在个股行情中)
        try:
           bm_ret = store.get_benchmark("000300", start=full_start)
           if not bm_ret.empty:
               # get_benchmark 返回 pd.Series(date → decimal return)
               prims["benchmark_ret"] = bm_ret
               _log.info("factor_cache: benchmark_ret loaded (%d dates, %.1f%% → %.1f%%)",
                         len(bm_ret), bm_ret.iloc[0]*100, bm_ret.iloc[-1]*100)
        except Exception as _e:
           _log.warning("factor_cache: benchmark_ret not available (%s), "
                        "residual_momentum_126d/idio_vol_126d will skip", _e)

       # 2.5 预加载 ztd 缓存 (ztd/zt_streak 等涨跌停因子依赖)
        preload_ztd_cache(date_range, symbols)
        _log.info("factor_cache: ztd cache preloaded (%d dates × %d symbols)", len(date_range), len(symbols))

        # 2.6 预加载 fundamentals (基本面因子需要)
        _store_fundamentals = {}
        for date_str in date_range:
            _store_fundamentals[date_str] = store.get_fundamentals(symbols, date=date_str)
        _log.info("factor_cache: fundamentals ready (%d dates)", len(_store_fundamentals))

        # 3. 逐日计算因子值 + 截面 zscore
        if force:
            c = self._get_conn()
            c.execute("DELETE FROM factor_values")
            c.commit()

        total_rows = 0
        n_dates_computed = 0
        batch = []
        batch_size = 5000

        for date_str in date_range:
            # 查该日期已有因子, 只算缺失的 (避免重复计算已有数据)
            existing = self._get_existing_factors(date_str)
            missing = [f for f in factor_names if f not in existing]
            if not missing:
                continue

            # 从 pre-loaded data 中提取当天切片
# 从 pre-loaded data 中提取当天切片
            try:
                ts = pd.Timestamp(date_str)
                if ts not in data_full.index:
                    continue
                day_data = data_full.loc[[ts]]
                if day_data.empty:
                    continue
            except Exception:
                continue

            # 计算因子值
            fv = compute_all_factors(
                day_data, date_str,
                primitives=prims,
               fundamentals=_store_fundamentals.get(date_str),
               factor_names=missing,
               status_filter=None,
                factor_fail_fast=False,  # 批量物化: 单个因子数据缺失不阻塞全量
            )

            # 截面 zscore + 写入 batch
            for fname, series in fv.items():
                if not isinstance(series, pd.Series) or series.dropna().empty:
                    continue
                vals = series.dropna()
                z = (vals - vals.mean()) / (vals.std() + 1e-10)
                for sym in vals.index:
                    batch.append((date_str, sym, fname,
                                  float(vals[sym]), round(float(z[sym]), 6)))
                    if len(batch) >= batch_size:
                        self._flush_batch(batch)
                        total_rows += len(batch)
                        batch = []
            n_dates_computed += 1

        if batch:
            self._flush_batch(batch)
            total_rows += len(batch)

        elapsed = _time.time() - t0
        _log.info("factor_cache: materialized %d dates × %d factors × %d symbols → %d rows in %.1fs",
                  n_dates_computed, len(factor_names), len(symbols), total_rows, elapsed)

        # 记录物化日志
        self._log_materialization(start_dt, end_dt, len(factor_names), len(symbols),
                                  n_dates_computed, total_rows, elapsed, force)
        return {"n_dates": n_dates_computed, "n_factors": len(factor_names),
                "n_symbols": len(symbols), "n_rows": total_rows, "elapsed_sec": round(elapsed, 1)}

    def _flush_batch(self, batch: list):
        c = self._get_conn()
        c.executemany(
            "INSERT OR REPLACE INTO factor_values(date, symbol, factor, raw_value, zscore) "
            "VALUES (?, ?, ?, ?, ?)", batch)
        c.commit()

    def _log_materialization(self, start, end, n_factors, n_symbols, n_dates, n_rows, elapsed, force):
        c = self._get_conn()
        c.execute(
            "INSERT INTO materialization_log(date_start, date_end, n_factors, n_symbols, n_dates, n_rows, elapsed_sec, force) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (start, end, n_factors, n_symbols, n_dates, n_rows, round(elapsed, 1), int(force)))
        c.commit()

    def is_materialized(self, date_range: list[str], factor_names: list[str]) -> bool:
        """检查最新日期是否覆盖了全部因子。

        只检查 date_range[-1] 而非逐日期遍历: 增量物化是顺序的, 最新日期覆盖
        了全部因子意味着历史日期也已覆盖。因子池变更 (新增因子) 时最新日期缺少
        新因子数据 → 返回 False → 触发补算。
        场景 7 (数据回补) 和场景 8 (因子逻辑修正) 仍需用户手动 force=True。
        """
        return self._date_has_data(date_range[-1], factor_names)

    def _date_has_data(self, date_str: str, factor_names: list[str]) -> bool:
        """检查该日期是否所有因子都有数据 (COUNT(DISTINCT factor) == len(factor_names))。

        之前用 COUNT(*)>0 的 bug: 只要任意一个因子有数据就跳过该日期,
        因子池新增因子时旧日期的数据永不补算。
        """
        return len(self._get_existing_factors(date_str)) == len(factor_names)

    def _get_existing_factors(self, date_str: str) -> set:
        """返回该日期已物化的因子名集合。"""
        c = self._get_conn()
        rows = c.execute(
            "SELECT DISTINCT factor FROM factor_values WHERE date=?", (date_str,)
        ).fetchall()
        return {r[0] for r in rows}

    def _get_existing_factors(self, date_str: str) -> set:
        """返回该日期已物化的因子名集合。"""
        c = self._get_conn()
        rows = c.execute(
            "SELECT DISTINCT factor FROM factor_values WHERE date=?", (date_str,)
        ).fetchall()
        return {r[0] for r in rows}
