"""因子值物化存储 — parquet 列式替代 SQLite (ADR-039 v2).

设计原则:
  - 每日期一个 gzip CSV 文件: factor_cache/YYYY-MM-DD.csv.gz
  - 列: symbol,factor,value (纯文本, gzip 高压缩率)
  - SQLite 3,768 MB → gzip CSV ~230 MB (94% 缩减)
  - 零外部依赖 (只需 Python stdlib gzip)
  - 与旧 SQLite 版本 API 完全兼容

对标: VN.PY 信号表(parquet) + DolphinDB 因子数据库
"""

import os
import gzip
import io
import inspect
import json
import tempfile
import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger
from quant.factor.compute.price._alternative import preload_ztd_cache
from quant.factor.compute._preload import preload_aux_data_chunk

_log = get_logger("factor.store")

# 项目根目录
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CACHE_DIR = os.path.join(_PROJ_ROOT, "quant", "data", "factor_cache")
_PARQUET_DIR = os.path.join(_CACHE_DIR, "parquet")
_LOG_FILE = os.path.join(_CACHE_DIR, "materialization_log.jsonl")

# 确保 parquet 目录存在
os.makedirs(_PARQUET_DIR, exist_ok=True)


# ── P1b: 因子源码 hash — 检测函数变更, 触发缓存重算 ──

_SOURCE_HASH_CACHE: dict[str, str] = {}


def _compute_factor_source_hash(factor_names: set[str]) -> str:
    """计算因子函数源码的复合 hash。

    从 _PRICE_FN_MAP / _FUNDAMENTAL_FN_MAP 取函数源码, 做 sha256。
    结果缓存 (同进程内因子源码不变)。
    """
    import hashlib
    from quant.factor.compute.price import _PRICE_FN_MAP
    from quant.factor.compute.fundamental import _FUNDAMENTAL_FN_MAP

    cache_key = ",".join(sorted(factor_names))
    if cache_key in _SOURCE_HASH_CACHE:
        return _SOURCE_HASH_CACHE[cache_key]

    h = hashlib.sha256()
    for name in sorted(factor_names):
        fn = None
        if name in _PRICE_FN_MAP:
            fn = _PRICE_FN_MAP[name][0]
        elif name in _FUNDAMENTAL_FN_MAP:
            fn = _FUNDAMENTAL_FN_MAP[name][1]
        if fn is not None:
            try:
                src = inspect.getsource(fn)
                h.update(src.encode())
            except (OSError, TypeError):
                h.update(name.encode())
    result = h.hexdigest()[:16]
    _SOURCE_HASH_CACHE[cache_key] = result
    return result




class FactorStore:
    """因子值物化存储 — gzip CSV 后端。

    使用流程:
      1. store.materialize(date_range, factor_names) → 批量计算并写入
      2. store.load(date, symbols, factor_names) → {factor_name: Series(symbol→value)}
      3. store.is_materialized(date_range, factor_names) → bool
    """

    def __init__(self, db_path: str = None):
        # db_path: 向后兼容旧 SQLite 路径。传入时使用该路径的父目录 + /factor_cache/。
        # 不传时使用默认 quant/data/factor_cache/。
        if db_path and db_path != _CACHE_DIR:
            parent = os.path.dirname(db_path)
            self._cache_dir = os.path.join(parent, "factor_cache")
        else:
            self._cache_dir = _CACHE_DIR
        self._parquet_dir = os.path.join(self._cache_dir, "parquet")
        os.makedirs(self._cache_dir, exist_ok=True)
        os.makedirs(self._parquet_dir, exist_ok=True)

    def _path(self, date_str: str) -> str:
        # 兼容旧 CSV
        return os.path.join(self._cache_dir, f"{date_str}.csv.gz")

    def _parquet_path(self, date_str: str, factor_name: str = None) -> str:
        """Parquet 分区路径: parquet/date=YYYY-MM-DD/factor_name.parquet"""
        base = os.path.join(self._parquet_dir, f"date={date_str}")
        if factor_name:
            return os.path.join(base, f"{factor_name}.parquet")
        return base

    def _manifest_path(self, date_str: str) -> str:
        return os.path.join(self._cache_dir, f"{date_str}.manifest.json")

    def close(self):
        pass  # no persistent connections

    # ── 物化 ──

    def materialize(self,
                    date_range: list[str],
                    factor_names: list[str],
                    symbols: list[str],
                    store: "DataStore" = None,
                    force: bool = False,
                    chunk_days: int = 200,
                    workers: int = None) -> dict:
        """批量物化因子值: 多进程并行按日期分片计算, 写入 Parquet 分区。

        Args:
            date_range: 交易日列表
            factor_names: 因子名列表
            symbols: 股票池
            store: DataStore 实例
            force: True 时删除旧数据重新物化
            chunk_days: 每块最大交易日数 (控制内存, 默认200天=~5GB/块)
            workers: 并行工作进程数 (默认 CPU 核心数 // 2, 最大 4)

        Returns:
            dict: {n_dates, n_factors, n_symbols, n_rows, elapsed_sec}

        Parallel: 使用 multiprocessing.Pool 并行处理日期, 共享数据通过临时 Parquet 传递。
        """
        import time as _time
        import gc as _gc
        from quant.factor.compute._dispatch import compute_all_factors
        from quant.factor.compute._primitives import precompute_primitives
        from quant.data.store import DataStore
        from quant.factor.windows import max_factor_calendar_days
        from quant.config.constants import _require_cfg

        # 默认并行度: CPU 核心数 // 2, 最大 4
        if workers is None:
            workers = min(mp.cpu_count() // 2, 4)
            workers = max(1, workers)

        _store_owned = store is None
        if store is None:
            store = DataStore()

        # 清理残留 WAL, 防止前次崩溃导致 "disk image is malformed"
        try:
            mconn = store._connect()
            mconn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as _e:
            _log.debug("WAL checkpoint failed (non-fatal): %s", _e)

        t0 = _time.time()

        # 0. 检查已有覆盖
        if not force and self.is_materialized(date_range, factor_names):
            _log.info("factor_cache: all dates already materialized, skip")
            return {"n_dates": len(date_range), "n_factors": len(factor_names),
                    "n_symbols": len(symbols), "n_rows": 0, "elapsed_sec": 0,
                    "skipped": True}

        # 0.3 ADR-039: 过滤到 stocks 表中的有效 symbol
        from quant.data.repos._base import DatabaseManager
        mconn = DatabaseManager.market()
        valid_syms = set(r[0] for r in mconn.execute(
            "SELECT DISTINCT symbol FROM stocks"
        ).fetchall())
        try:
            mconn.close()
        except Exception as _e:
            _log.debug("mconn close failed (non-fatal): %s", _e)
        symbols_filtered = [s for s in symbols if s in valid_syms]
        _log.info("factor_cache: symbol filter %d → %d (stocks table)",
                  len(symbols), len(symbols_filtered))
        symbols = symbols_filtered
        if not symbols:
            _log.warning("factor_cache: no valid symbols after filtering, abort")
            return {"n_dates": 0, "n_factors": 0, "n_symbols": 0, "n_rows": 0,
                    "elapsed_sec": 0, "skipped": True}

        # 0.4 确定分块策略: 每块最多 chunk_days 个交易日
        eff_days = max(_require_cfg("data.lookback_days"), max_factor_calendar_days(None))
        dates = sorted(date_range)
        n_total = len(dates)
        chunk_size = chunk_days

        if force:
            for f in os.listdir(self._cache_dir):
                if f.endswith('.csv.gz') or f.endswith('.manifest.json') or f.endswith('.parquet'):
                    os.remove(os.path.join(self._cache_dir, f))
            # 也清理 parquet 目录
            import shutil
            if os.path.exists(self._parquet_dir):
                shutil.rmtree(self._parquet_dir)
            os.makedirs(self._parquet_dir, exist_ok=True)

        total_rows = 0
        n_dates_computed = 0
        n_chunks = (n_total + chunk_size - 1) // chunk_size

        _log.info("factor_cache: %d total dates → %d chunks (≤%d dates/chunk, %dd lookback, workers=%d)",
                  n_total, n_chunks, chunk_size, eff_days, workers)

        # 收集所有需要计算的日期 (跳过已完全缓存的)
        dates_to_compute = []
        for date_str in dates:
            if not force and self._date_has_all_factors(date_str, factor_names):
                continue
            dates_to_compute.append(date_str)

        if not dates_to_compute:
            _log.info("factor_cache: all dates already fully materialized, skip")
            return {"n_dates": len(date_range), "n_factors": len(factor_names),
                    "n_symbols": len(symbols), "n_rows": 0, "elapsed_sec": 0,
                    "skipped": True}

        _log.info("factor_cache: %d dates need computation (total %d, workers=%d)",
                  len(dates_to_compute), len(dates), workers)

        # 准备共享数据: 写入临时 Parquet 供工作进程读取
        with tempfile.TemporaryDirectory() as tmpdir:
            t_prep = _time.time()
            
            # 确定最早需要的数据起始日期
            earliest_date = dates_to_compute[0]
            data_start = (pd.Timestamp(earliest_date) - pd.Timedelta(days=eff_days)).strftime("%Y-%m-%d")
            latest_date = dates_to_compute[-1]
            
            _log.info("factor_cache: loading data %s → %s (lookback %dd)", data_start, latest_date, eff_days)
            data_full = store.get_daily(symbols, start=data_start, end=latest_date)
            t1 = _time.time()
            _log.info("factor_cache: loaded %d days × %d symbols (%.1fs)", len(data_full), len(symbols), t1 - t_prep)
            
            # 预计算原语
            prims = precompute_primitives(data_full, factor_names=factor_names)
            t2 = _time.time()
            _log.info("factor_cache: primitives computed (%.1fs)", t2 - t1)
            
            # 基准收益率
            try:
                bm_ret = store.get_benchmark("000300", start=data_start)
                if not bm_ret.empty:
                    prims["benchmark_ret"] = bm_ret
            except Exception as _e:
                _log.warning("factor_cache: benchmark_ret not available (%s)", _e)
            
            # 预加载 ZTD 缓存
            preload_ztd_cache(dates_to_compute, symbols)
            
            # 预加载 aux 数据
            aux_full = preload_aux_data_chunk(symbols, earliest_date, latest_date)
            
            # 预加载基本面
            chunk_fundamentals = self._build_fundamentals_panel(
                store, symbols, dates_to_compute, data_full=data_full
            )
            t3 = _time.time()
            _log.info("factor_cache: aux+fundamentals ready (%.1fs)", t3 - t2)
            
            # 写入临时文件供 worker 读取
            _log.info("factor_cache: writing shared data to temp dir...")
            data_full.to_parquet(os.path.join(tmpdir, "data_full.parquet"))
            pd.to_pickle(prims, os.path.join(tmpdir, "prims.pkl"))
            pd.to_pickle(aux_full, os.path.join(tmpdir, "aux_full.pkl"))
            pd.to_pickle(chunk_fundamentals, os.path.join(tmpdir, "fundamentals.pkl"))
            with open(os.path.join(tmpdir, "meta.json"), "w") as f:
                json.dump({
                    "symbols": symbols,
                    "factor_names": factor_names,
                    "eff_days": eff_days,
                    "earliest_date": earliest_date,
                    "latest_date": latest_date,
                }, f)
            
            # 定义 worker 函数 (顶层函数才能被 pickle)
            # 这里用静态方法避免序列化 self
            from quant.factor.compute._preload import slice_aux_for_date
            
            def _worker_compute_date(args):
                """Worker 进程计算单日因子"""
                date_str, tmpdir, factor_names = args
                try:
                    # 读取共享数据
                    data_full = pd.read_parquet(os.path.join(tmpdir, "data_full.parquet"))
                    import pandas as pd
                    prims = pd.read_pickle(os.path.join(tmpdir, "prims.pkl"))
                    aux_full = pd.read_pickle(os.path.join(tmpdir, "aux_full.pkl"))
                    chunk_fundamentals = pd.read_pickle(os.path.join(tmpdir, "fundamentals.pkl"))
                    
                    existing = set()  # worker 中不检查缓存，由主进程过滤
                    missing = factor_names
                    
                    from quant.factor.compute._preload import slice_aux_for_date
                    from quant.factor.compute._dispatch import compute_all_factors
                    
                    _aux_sliced = slice_aux_for_date(aux_full, date_str)
                    _fund = chunk_fundamentals.get(date_str)
                    
                    fv = compute_all_factors(
                        data_full, date_str,
                        primitives=prims,
                        fundamentals=_fund,
                        preloaded_aux_chunk=_aux_sliced,
                        factor_names=missing,
                        status_filter=None,
                        factor_fail_fast=False,
                        quiet=True,
                        financials_cache={},
                    )
                    
                    lines = []
                    for fname, series in fv.items():
                        if not isinstance(series, pd.Series) or series.dropna().empty:
                            continue
                        for sym, val in series.dropna().items():
                            lines.append(f"{sym},{fname},{val:.6f}")
                    return date_str, lines
                except Exception as e:
                    import traceback
                    return date_str, f"ERROR: {traceback.format_exc()}"
            
            # 并行计算
            _log.info("factor_cache: starting parallel computation with %d workers...", workers)
            t_compute_start = _time.time()
            
            # 准备任务参数
            task_args = [(ds, tmpdir, factor_names) for ds in dates_to_compute]
            
            chunk_new_rows = {}
            with mp.Pool(processes=workers) as pool:
                # 使用 imap_unordered 获取结果
                for i, result in enumerate(pool.imap_unordered(_worker_compute_date, task_args), 1):
                    date_str, lines = result
                    if isinstance(lines, str) and lines.startswith("ERROR"):
                        _log.error("factor_cache: %s compute failed: %s", date_str, lines)
                        continue
                    if lines:
                        chunk_new_rows[date_str] = lines
                    if i % 20 == 0 or i == len(dates_to_compute):
                        _log.info("  parallel compute: %d/%d dates done (workers=%d)", i, len(dates_to_compute), workers)
            
            t_compute_end = _time.time()
            _log.info("factor_cache: parallel compute done (%.1fs)", t_compute_end - t_compute_start)
            
            # 批量写入 Parquet
            chunk_rows = self._write_chunk_rows(chunk_new_rows, factor_names)
            total_rows += chunk_rows
            n_dates_computed = len(chunk_new_rows)
            
            _log.info("factor_cache: wrote %d rows for %d dates (%.1fs)",
                      chunk_rows, n_dates_computed, _time.time() - t_compute_end)

        elapsed = _time.time() - t0
        _log.info("factor_cache: materialized %d dates × %d factors × %d symbols → %d rows in %.1fs (workers=%d)",
                  n_dates_computed, len(factor_names), len(symbols), total_rows, elapsed, workers)

        self._log_materialization(dates[0], dates[-1], len(factor_names), len(symbols),
                                  n_dates_computed, total_rows, elapsed, force)

        # 关闭内部创建的 DataStore, 释放 SQLite 连接
        if _store_owned:
            try:
                store.close()
            except Exception as _e:
                _log.debug("store close failed (non-fatal): %s", _e)

        # 统计 Parquet 缓存大小
        size_mb = 0
        for root, dirs, files in os.walk(self._parquet_dir):
            for f in files:
                if f.endswith('.parquet'):
                    size_mb += os.path.getsize(os.path.join(root, f))
        size_mb /= 1024 * 1024
        _log.info("factor_cache: total parquet cache size %.0f MB", size_mb)

        # P1: 物化完成 → 释放 ztd 预计算缓存 (~80MB for 2000 dates)
        from quant.factor.compute.price._alternative import clear_ztd_cache
        clear_ztd_cache()

        return {"n_dates": n_dates_computed, "n_factors": len(factor_names),
                "n_symbols": len(symbols), "n_rows": total_rows,
                "elapsed_sec": round(elapsed, 1)}

    # ── 读取 ──

    def load(self, date_str: str, symbols=None, factor_names=None) -> dict:
        """从缓存读取单日因子值。返回 {factor_name: pd.Series(symbol→value)}.
        优先读 Parquet 分区，回退兼容旧 gzip CSV。
        """
        # 1) 尝试读 Parquet 分区
        pdir = self._parquet_path(date_str)
        if os.path.exists(pdir):
            try:
                filters = []
                if factor_names:
                    filters.append(("factor", "in", factor_names))
                df = pd.read_parquet(pdir, filters=filters)
                if df.empty:
                    return {}
                if symbols:
                    df = df[df["symbol"].isin(symbols)]
                # 转为 {factor: Series}
                result = {}
                for fname, gdf in df.groupby("factor"):
                    result[fname] = pd.Series(gdf["value"].values, index=gdf["symbol"], name=fname)
                return result
            except Exception as e:
                _log.warning(f"factor_cache: parquet read failed for {date_str}, fallback to CSV: {e}")

        # 2) 回退 CSV
        path = self._path(date_str)
        if not os.path.exists(path):
            return {}

        lines = self._read_raw_lines(date_str)
        if not lines:
            return {}

        result = {}
        for line in lines:
            parts = line.split(",")
            if len(parts) < 3:
                continue
            sym, factor = parts[0], parts[1]
            val_str = parts[2]  # value is 3rd column (date may be 4th, ignored)
            if factor_names and factor not in factor_names:
                continue
            if symbols and sym not in symbols:
                continue
            try:
                val = float(val_str)
            except ValueError:
                continue
            result.setdefault(factor, {})[sym] = val

        return {fn: pd.Series(data, name=fn) for fn, data in result.items() if data}

    def bulk_load(self, dates: list[str], symbols=None, factor_names=None) -> dict[str, dict]:
        """test-v397 (P0): 批量加载多日因子值到内存 dict。

        Returns: {date_str: {factor_name: pd.Series(symbol→value)}}
        用途: 回测启动时一次性加载全量, 消除逐日的 gzip I/O。
        244 天 × 30 因子 × 800 股 ≈ 47 MB, 内存完全可接受。
        优先读 Parquet 分区 (列式投影 + 谓词下推)，回退 CSV。
        """
        cache: dict[str, dict] = {}
        n = len(dates)

        # 并行读取：ThreadPoolExecutor 8 线程
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=8) as _ex:
            _futures = {_ex.submit(self.load, ds, symbols, factor_names): ds for ds in dates}
            for i, _fu in enumerate(as_completed(_futures), 1):
                ds = _futures[_fu]
                fv = _fu.result()
                if fv:
                    cache[ds] = fv
                if i % max(1, n // 10) == 0 or i == n:
                    _log.info("bulk_load: %d/%d dates loaded (%d factors avg, threads=8)",
                              i, n, sum(len(v) for v in cache.values()) // max(len(cache), 1))
        return cache

    def _read_raw_lines(self, date_str: str) -> list[str]:
        path = self._path(date_str)
        if not os.path.exists(path):
            return []
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]

    def _write_chunk_rows(self, chunk_rows: dict[str, list[str]], factor_names: list[str]) -> int:
        """批量写一个 chunk 的 CSV 缓存 (A3 + C2 manifest)。

        对已有文件读→合并→压缩→写; 新文件直接压缩写。
        test-v403: 
        - 过滤 factor_names 外的因子行 (防止 archived 因子残留)
        - 同 key 新值覆盖旧值 (因子代码修复后缓存值应更新)
        写完后更新 manifest, 记录该日期已物化的因子集合。
        返回实际写入的总行数。
        """
    def _write_chunk_rows(self, chunk_rows: dict[str, list[str]], factor_names: list[str]) -> int:
        """批量写一个 chunk 的 Parquet 缓存 (分区: date=YYYY-MM-DD/factor_name.parquet)。

        - 按因子分文件写入，列式压缩 (ZSTD level 3)
        - 自动去重：同 (symbol, factor, date) 保留最后一条
        - 更新 manifest 记录已物化因子集合
        返回实际写入的总行数。
        """
        import pandas as pd
        total = 0
        for date_str, lines in chunk_rows.items():
            if not lines:
                continue

            # 解析行，构建 DataFrame
            records = []
            for line in lines:
                parts = line.split(",", 2)
                if len(parts) < 3:
                    continue
                sym, fname, val_str = parts[0], parts[1], parts[2]
                if fname not in factor_names:
                    continue
                try:
                    val = float(val_str)
                except ValueError:
                    continue
                records.append((sym, fname, val, date_str))

            if not records:
                continue

            df = pd.DataFrame(records, columns=["symbol", "factor", "value", "date"])
            df["value"] = df["value"].astype("float32")  # 节省空间

            # 去重：同 (symbol, factor, date) 保留最后一条
            df = df.drop_duplicates(subset=["symbol", "factor", "date"], keep="last")

            # 按因子分组写入各自分区文件
            for fname, fdf in df.groupby("factor"):
                if fname not in factor_names:
                    continue
                pdir = self._parquet_path(date_str, fname)
                os.makedirs(os.path.dirname(pdir), exist_ok=True)
                # 如果已存在，读取合并去重
                if os.path.exists(pdir):
                    existing = pd.read_parquet(pdir)
                    combined = pd.concat([existing, fdf], ignore_index=True)
                    combined = combined.drop_duplicates(subset=["symbol", "factor", "date"], keep="last")
                    fdf = combined
                fdf.to_parquet(pdir, compression="zstd", compression_level=3, index=False)

            # C2: 更新 manifest
            factors = set(df["factor"].unique())
            self._write_manifest(date_str, factors, len(df))
            total += len(df)
        return total

    def _build_fundamentals_panel(self, store, symbols: list[str],
                                  chunk_dates: list[str],
                                  data_full: pd.DataFrame = None) -> dict[str, pd.DataFrame]:
        """构建基本面 PIT panel, 返回 {date_str: DataFrame(symbol×field)}。

        B2 向量化实现: pivot + ffill 一次生成整块 panel,
        避免原逐日循环中的重复 pivot/loc。

        data_full: 若传入 (MultiIndex field×symbol), 复用其 close 面板,
        跳过 daily 表的独立 SQL 查询 (v366 性能优化)。
        """
        if not chunk_dates:
            return {}

        val_start = chunk_dates[0]
        val_end = chunk_dates[-1]
        mconn = store._connect()
        ph_stocks = ",".join("?" * len(symbols))

        val_df = pd.read_sql_query(
            "SELECT symbol, date, pe_ttm, pb, ps_ttm, pcf_ttm, market_cap FROM daily_valuation "
            "WHERE date >= ? AND date <= ? ORDER BY date",
            mconn, params=(val_start, val_end)
        )
        stocks_df = pd.read_sql_query(
            f"SELECT symbol, pe, pe_ttm, pb, total_mv, roe, industry, high_52w, eps, bvps "
            f"FROM stocks WHERE symbol IN ({ph_stocks})",
            mconn, params=symbols
        ).set_index("symbol")
        daily_df = pd.read_sql_query(
            f"SELECT symbol, date, close FROM daily "
            f"WHERE symbol IN ({ph_stocks}) AND date >= ? AND date <= ? ORDER BY date",
            mconn, params=symbols + [val_start, val_end]
        )

        # PIT 估值 panel
        if not val_df.empty:
            val_df["date"] = pd.to_datetime(val_df["date"])
            val_piv = val_df.pivot(index="date", columns="symbol",
                                   values=["pe_ttm", "pb", "market_cap"]).ffill()
        else:
            val_piv = None

        # PIT close panel + 52周高 (v366: 优先复用 data_full["close"], 跳过一次 SQL+pivot+ffill)
        if data_full is not None and "close" in data_full.columns.levels[0]:
            close_piv = data_full["close"]
            high_52w = close_piv.rolling(244, min_periods=60).max()
        elif not daily_df.empty:
            daily_df["date"] = pd.to_datetime(daily_df["date"])
            close_piv = daily_df.pivot(index="date", columns="symbol",
                                       values="close").ffill()
            high_52w = close_piv.rolling(244, min_periods=60).max()
        else:
            close_piv = None
            high_52w = None

        # 预提取静态列 (逐日不变), 避免 per-date stocks_df.copy()
        _static_cols = {c: stocks_df[c] for c in stocks_df.columns
                        if c not in ("pe_ttm", "pb", "market_cap", "close_latest", "high_52w")}
        _static_index = stocks_df.index
        # 回退值: val_piv 不可用时用 stocks 静态值
        _fallback = {c: stocks_df[c] for c in ("pe_ttm", "pb", "market_cap")
                     if c in stocks_df.columns}

        result = {}
        for date_str in chunk_dates:
            ts = pd.Timestamp(date_str)
            _dyn = dict(_fallback)  # 从回退值开始

            if val_piv is not None and ts in val_piv.index:
                row = val_piv.loc[ts]
                for col in ["pe_ttm", "pb", "market_cap"]:
                    if col in row.index.get_level_values(0):
                        _dyn[col] = row[col].reindex(_static_index)

            if close_piv is not None and ts in close_piv.index:
                _dyn["close_latest"] = close_piv.loc[ts].reindex(_static_index)
            if high_52w is not None and ts in high_52w.index:
                _dyn["high_52w"] = high_52w.loc[ts].reindex(_static_index)

            df = pd.DataFrame({**_static_cols, **_dyn}, index=_static_index)

            # derive ROE from PB/PE_TTM (PIT dynamic pe_ttm, not static stocks.pe)
            # 来源: JQData daily_valuation.pe_ttm (ADR-035 审计确认); pb/pe_ttm = E/B = ROE
            # pe_ttm 在 _dyn 中由 val_piv 动态注入, pe 来自 stocks 静态表
            null_roe = df["roe"].isna() | (df["roe"] <= 0)
            if null_roe.any():
                pe_col = "pe_ttm" if "pe_ttm" in df.columns else "pe"
                derived = df["pb"] / df[pe_col].replace(0, None)
                derived = derived.where((derived > 0) & (derived < 100))
                df.loc[null_roe, "roe"] = derived.loc[null_roe]

            # filter extreme PE/PB
            df.loc[df["pe"] <= 0, "pe"] = None
            df.loc[df["pe"] > 1000, "pe"] = None
            df.loc[df["pb"] <= 0, "pb"] = None

            result[date_str] = df

        return result

    def _factor_needs_worker(self, name: str) -> bool:
        """判断因子是否走多进程 worker (非纯 shortcut 因子)。"""
        from quant.factor.compute.price import _PRICE_FN_MAP
        from quant.factor.compute._primitives import FACTOR_SHORTCUT
        entry = _PRICE_FN_MAP.get(name)
        if not entry:
            return True
        fn, _ = entry
        return fn.__name__ not in FACTOR_SHORTCUT

    def _compute_date(self, date_str: str, data_full: pd.DataFrame,
                      prims: dict, fundamentals: pd.DataFrame | None,
                      aux_full: dict, factor_names: list[str]) -> list[str]:
        """计算单日期因子, 返回 CSV 行列表。"""
        from quant.factor.compute._dispatch import compute_all_factors
        try:
            ts = pd.Timestamp(date_str)
            if ts not in data_full.index:
                return []
            day_data = data_full.loc[:ts]
            if day_data.empty:
                return []
        except Exception:
            return []

        fv = compute_all_factors(
            day_data, date_str,
            primitives=prims,
            fundamentals=fundamentals,
            preloaded_aux_chunk=aux_full,
            factor_names=factor_names,
            status_filter=None,
            factor_fail_fast=False,
            quiet=True,
            financials_cache={},
        )

        lines = []
        for fname, series in fv.items():
            if not isinstance(series, pd.Series) or series.dropna().empty:
                continue
            for sym, val in series.dropna().items():
                lines.append(f"{sym},{fname},{val:.6f}")
        return lines

    # ── 查询 ──

    def _get_existing_factors(self, date_str: str) -> set:
        """返回该日期已物化的因子名集合。

        C2: 优先读 manifest, 不存在时回退扫描 gzip CSV (兼容旧缓存)。
        test-v403: manifest source_hash 不匹配 → 返回空集合 → 触发重算。
        旧行为 (v398) 只 log 不重算, 导致因子代码修复后缓存值永久过时。
        """
        mpath = self._manifest_path(date_str)
        if os.path.exists(mpath):
            try:
                with open(mpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                factors = set(data.get("factors", []))
                if factors and "source_hash" in data:
                    current_hash = _compute_factor_source_hash(factors)
                    if data["source_hash"] != current_hash:
                        _log.info("factor_cache: %s source_hash changed → invalidating (%s→%s)",
                                  date_str, data["source_hash"][:8], current_hash[:8])
                        return set()  # test-v403: 触发重算
                return factors
            except Exception as e:
                _log.warning("factor_cache: manifest read failed for %s: %s", date_str, e)

        path = self._path(date_str)
        if not os.path.exists(path):
            return set()
        factors = set()
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",", 2)
                if len(parts) >= 2:
                    factors.add(parts[1])
                if len(factors) > 200:  # optimization: early exit
                    break
        return factors

    def _write_manifest(self, date_str: str, factors: set[str], n_rows: int):
        """写入日期级因子清单, 含 source_hash (P1b 变更检测)."""
        try:
            record = {
                "factors": sorted(factors),
                "n_rows": n_rows,
                "source_hash": _compute_factor_source_hash(factors),
                "updated_at": pd.Timestamp.now().isoformat(),
            }
            with open(self._manifest_path(date_str), 'w', encoding='utf-8') as f:
                json.dump(record, f)
        except Exception as e:
            _log.warning("factor_cache: manifest write failed for %s: %s", date_str, e)

    def _scan_file_factors(self, filename: str) -> set:
        path = os.path.join(self._cache_dir, filename)
        factors = set()
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",", 2)
                if len(parts) >= 2:
                    factors.add(parts[1])
                if len(factors) > 200:
                    break
        return factors

    def is_materialized(self, date_range: list[str], factor_names: list[str]) -> bool:
        """检查是否所有日期都覆盖了全部因子 (不只最新日期)。"""
        if not date_range:
            return False
        # 抽查策略: 最新 + 最早 + 每隔 N 天抽一个
        check_dates = [date_range[0], date_range[-1]]
        step = max(1, len(date_range) // 20)
        for i in range(step, len(date_range) - 1, step):
            check_dates.append(date_range[i])
        return all(self._date_has_all_factors(d, factor_names) for d in check_dates)

    def _date_has_data(self, date_str: str, _factor_names_hint: list[str] | None = None) -> bool:
        """回测用: 检查 gzip 文件物理存在 (不管里面有几个因子)。"""
        import os
        return os.path.exists(self._path(date_str))

    def _date_has_all_factors(self, date_str: str, factor_names: list[str]) -> bool:
        """物化用: 检查日期是否覆盖了全部入口因子 (用于判断是否需要重算)。"""
        return len(self._get_existing_factors(date_str)) >= len(factor_names)

    # ── 维护 ──

    def trim_to_max_days(self, max_days: int) -> int:
        """删除超过 max_days 天前的旧缓存文件。

        max_days: 保留的交易日数 (如 2000 ≈ 2000×365/244 ≈ 2990 日历日)。
        cutoff 按交易日→日历日转换: max_days * 365 // 244。
        锚点: 缓存中最新的日期 (非系统时间), 避免物化期间误删。
        """
        if max_days <= 0:
            return 0
        # 找到缓存中最新的日期作为锚点
        latest = None
        for f in sorted(os.listdir(self._cache_dir), reverse=True):
            if f.endswith('.csv.gz'):
                latest = f.replace('.csv.gz', '')
                break
        if not latest:
            return 0
        cutoff = (pd.Timestamp(latest) - pd.Timedelta(days=max_days * 365 // 244)).strftime("%Y-%m-%d")
        _log.info("factor_cache: trim — anchor=%s, cutoff=%s (max_days=%d)",
                  latest, cutoff, max_days)
        deleted = 0
        for f in sorted(os.listdir(self._cache_dir)):
            if not f.endswith('.csv.gz'):
                continue
            date_str = f.replace('.csv.gz', '')
            if date_str < cutoff:
                os.remove(os.path.join(self._cache_dir, f))
                # test-v403: 同步删除 manifest, 防止孤立文件残留
                mf = self._manifest_path(date_str)
                if os.path.exists(mf):
                    os.remove(mf)
                deleted += 1
        if deleted:
            _log.info("factor_cache: trimmed %d files before %s", deleted, cutoff)
        return deleted

    def _log_materialization(self, start, end, n_factors, n_symbols,
                             n_dates, n_rows, elapsed, force):
        """写入物化日志 (JSONL)。"""
        try:
            record = {
                "ts": pd.Timestamp.now().isoformat(),
                "date_start": start, "date_end": end,
                "n_factors": n_factors, "n_symbols": n_symbols,
                "n_dates": n_dates, "n_rows": n_rows,
                "elapsed_sec": round(elapsed, 1), "force": bool(force),
            }
            with open(_LOG_FILE, 'a') as f:
                f.write(json.dumps(record) + "\n")
        except Exception as _e:
            _log.warning("factor_cache: failed to log materialization: %s", _e)

    # ── P3b: 断点续传 ──

    def _checkpoint_path(self) -> str:
        return os.path.join(self._cache_dir, "_checkpoint.json")

    def _write_checkpoint(self, last_date: str, chunk_done: int, n_chunks: int):
        """每块完成后写断点: {last_date, chunk_done, n_chunks}。"""
        try:
            record = {
                "last_date": last_date,
                "chunk_done": chunk_done,
                "n_chunks": n_chunks,
                "ts": pd.Timestamp.now().isoformat(),
            }
            with open(self._checkpoint_path(), 'w') as f:
                json.dump(record, f)
        except Exception as e:
            _log.debug(f"checkpoint write failed (non-fatal): {e}")

    def _read_checkpoint(self) -> dict | None:
        """读取上次物化的断点。不存在或超过 24h → None。"""
        cpath = self._checkpoint_path()
        if not os.path.exists(cpath):
            return None
        try:
            with open(cpath, 'r') as f:
                data = json.load(f)
            # 超过 24h 的断点视为过期
            ts = pd.Timestamp(data.get("ts", "1970-01-01"))
            if (pd.Timestamp.now() - ts).total_seconds() > _require_cfg("factor.compute.cache_checkpoint_ttl_sec"):
                os.remove(cpath)
                return None
            return data
        except Exception:
            return None

    def _clear_checkpoint(self):
        """物化完成/放弃后清理断点。"""
        try:
            cpath = self._checkpoint_path()
            if os.path.exists(cpath):
                os.remove(cpath)
        except Exception:
            pass
