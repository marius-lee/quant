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

        # 2.5 预加载 ztd 缓存 (ztd/zt_streak 等涨跌停因子依赖)
        preload_ztd_cache(date_range, symbols)
        _log.info("factor_cache: ztd cache preloaded (%d dates × %d symbols)", len(date_range), len(symbols))

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
            if not force and self._date_has_data(date_str, factor_names):
                continue

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
                factor_names=factor_names,
                status_filter=None,  # 算全部指定因子, 不受状态限制
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

    def _date_has_data(self, date_str: str, factor_names: list[str]) -> bool:
        c = self._get_conn()
        placeholders = ",".join("?" * len(factor_names))
        cnt = c.execute(
            f"SELECT COUNT(*) FROM factor_values WHERE date=? AND factor IN ({placeholders})",
            [date_str] + list(factor_names)
        ).fetchone()[0]
        return cnt > 0

    # ── 读取 ──

    def load(self, date: str, symbols: list[str] = None,
             factor_names: list[str] = None) -> dict:
        """加载某日的因子值 → {factor_name: Series(symbol→value)}。

        返回格式与 compute_all_factors() 一致, 可直接传入 AlphaModel.combine()。
        """
        import pandas as pd
        c = self._get_conn()

        if factor_names:
            placeholders = ",".join("?" * len(factor_names))
            rows = c.execute(
                f"SELECT symbol, factor, raw_value FROM factor_values "
                f"WHERE date=? AND factor IN ({placeholders})",
                [date] + list(factor_names)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT symbol, factor, raw_value FROM factor_values WHERE date=?",
                (date,)
            ).fetchall()

        if not rows:
            return {}

        # 按因子分组构建 Series
        from collections import defaultdict
        by_factor = defaultdict(dict)
        for sym, fname, val in rows:
            by_factor[fname][sym] = val

        result = {}
        for fname, sym_vals in by_factor.items():
            s = pd.Series(sym_vals, name=fname)
            if symbols:
                s = s.reindex(symbols)
            result[fname] = s

        return result

    def is_materialized(self, date_range: list[str], factor_names: list[str]) -> bool:
        """检查所有日期 × 因子的值是否都已物化。"""
        c = self._get_conn()
        placeholders = ",".join("?" * len(factor_names))
        for d in date_range:
            cnt = c.execute(
                f"SELECT COUNT(*) FROM factor_values WHERE date=? AND factor IN ({placeholders})",
                [d] + list(factor_names)
            ).fetchone()[0]
            if cnt == 0:
                return False
        return True

    def get_coverage(self, date_range: list[str], factor_names: list[str]) -> dict:
        """返回物化覆盖率统计。"""
        c = self._get_conn()
        placeholders = ",".join("?" * len(factor_names))
        total = len(date_range)
        covered = 0
        for d in date_range:
            cnt = c.execute(
                f"SELECT COUNT(*) FROM factor_values WHERE date=? AND factor IN ({placeholders})",
                [d] + list(factor_names)
            ).fetchone()[0]
            if cnt > 0:
                covered += 1
        return {"total_dates": total, "covered_dates": covered,
                "coverage_pct": round(covered / max(total, 1) * 100, 1)}

    def get_latest_materialization(self) -> dict | None:
        """返回最近一次物化的元数据。"""
        c = self._get_conn()
        row = c.execute(
            "SELECT run_ts, date_start, date_end, n_factors, n_symbols, n_dates, n_rows, elapsed_sec, force "
            "FROM materialization_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return dict(zip(["run_ts", "date_start", "date_end", "n_factors",
                             "n_symbols", "n_dates", "n_rows", "elapsed_sec", "force"], row))
        return None

    def invalidate(self, date_str: str = None):
        """使缓存失效。不传参数则清空全部。"""
        c = self._get_conn()
        if date_str:
            c.execute("DELETE FROM factor_values WHERE date=?", (date_str,))
        else:
            c.execute("DELETE FROM factor_values")
        c.commit()
        _log.info("factor_cache: invalidated %s", date_str or "ALL")
