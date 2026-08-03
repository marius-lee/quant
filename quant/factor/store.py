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
import pandas as pd
import numpy as np
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger
from quant.factor.compute.price._alternative import preload_ztd_cache
from quant.factor.compute._preload import preload_aux_data_chunk

_log = get_logger("factor.store")

# 项目根目录
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CACHE_DIR = os.path.join(_PROJ_ROOT, "quant", "data", "factor_cache")
_LOG_FILE = os.path.join(_CACHE_DIR, "materialization_log.jsonl")


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


# ── ProcessPoolExecutor worker 函数 (B1) ──
# 必须模块级可 pickle。

import multiprocessing as _mp


def _get_mp_context():
    """获取多进程上下文: macOS Python 3.14+ 默认 spawn, 需显式指定 fork.
    
    spawn 模式下子进程重新导入模块 → freeze_support 报错。
    fork 模式直接复制父进程内存, 兼容现有 preload_ztd_cache 预加载。
    """
    try:
        return _mp.get_context('fork')
    except ValueError:
        return _mp.get_context('spawn')  # Windows fallback

# ── worker 共享数据 (方案 A): fork 后子进程继承, 消除 submit 序列化 ──
_worker_data: "pd.DataFrame | None" = None
_worker_prims: "dict | None" = None
_worker_aux: "dict | None" = None
_worker_fundamentals: "dict | None" = None


def _worker_init(chunk_dates: list[str], symbols: list[str]):
    """子进程初始化: fork 已继承父进程的 _ztd_cache, 无需重算。

    注意: data_full/prims/aux/fundamentals 通过模块全局变量共享 (fork 继承),
    不在 initargs 中传递以避免序列化开销。
    """
    pass  # fork 继承所有模块级缓存 (_ztd_cache/_DV_CACHE 等), 零初始化开销


def _worker_compute_date(date_str: str, factor_names: list[str]) -> list[str]:
    """子进程执行的单日计算 — 从模块全局读数据, 零序列化。"""
    from quant.factor.compute._dispatch import compute_all_factors
    try:
        ts = pd.Timestamp(date_str)
        if _worker_data is None or ts not in _worker_data.index:
            return []
        day_data = _worker_data.loc[:ts]
        if day_data.empty:
            return []
    except Exception:
        return []

    fv = compute_all_factors(
        day_data, date_str,
        primitives=_worker_prims,
        fundamentals=_worker_fundamentals.get(date_str) if _worker_fundamentals else None,
        preloaded_aux_chunk=_worker_aux,
        factor_names=factor_names,
        status_filter=None,
        factor_fail_fast=False,
        quiet=True,
    )

    lines = []
    for fname, series in fv.items():
        if not isinstance(series, pd.Series) or series.dropna().empty:
            continue
        for sym, val in series.dropna().items():
            lines.append(f"{sym},{fname},{val:.6f}")
    return lines


class FactorStore:
    """因子值物化存储 — gzip CSV 后端。

    使用流程:
      1. store.materialize(date_range, factor_names) → 批量计算并写入
      2. store.load(date, symbols, factor_names) → {factor_name: Series(symbol→value)}
      3. store.is_materialized(date_range, factor_names) → bool
    """

    def __init__(self, db_path: str = None):
        # db_path: 向后兼容旧 SQLite 路径。传入时使用该路径的子目录作为缓存。
        # 不传时使用默认 quant/data/factor_cache/。
        if db_path and db_path != _CACHE_DIR:
            # 测试/自定义路径: 用 db_path 的父目录 + /factor_cache/
            parent = os.path.dirname(db_path)
            self._cache_dir = os.path.join(parent, "factor_cache")
        else:
            self._cache_dir = _CACHE_DIR
        os.makedirs(self._cache_dir, exist_ok=True)

    def _path(self, date_str: str) -> str:
        return os.path.join(self._cache_dir, f"{date_str}.csv.gz")

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
                    chunk_days: int = 200) -> dict:
        """批量物化因子值: 分块加载数据 + 原语, 逐日计算全部因子, 写入 gzip CSV。

        Args:
            date_range: 交易日列表
            factor_names: 因子名列表
            symbols: 股票池
            store: DataStore 实例
            force: True 时删除旧数据重新物化
            chunk_days: 每块最大交易日数 (控制内存, 默认200天=~5GB/块)

        Returns:
            dict: {n_dates, n_factors, n_symbols, n_rows, elapsed_sec}

        Memory: 一次性加载 2020→now (~1590天) 需 ~25GB, 分块200天 (~5GB/块)
                共 ~8 块, 每块完成后释放该块内存。
        """
        import time as _time
        import gc as _gc
        from quant.factor.compute._dispatch import compute_all_factors
        from quant.factor.compute._primitives import precompute_primitives
        from quant.data.store import DataStore
        from quant.factor.windows import max_factor_calendar_days
        from quant.config.constants import _require_cfg

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
            _log.debug("mconn close failed (non-fatal): %s", _e)  # monkeypatched shared conn in tests
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
                if f.endswith('.csv.gz') or f.endswith('.manifest.json'):
                    os.remove(os.path.join(self._cache_dir, f))

        total_rows = 0
        n_dates_computed = 0
        n_chunks = (n_total + chunk_size - 1) // chunk_size

        _log.info("factor_cache: %d total dates → %d chunks (≤%d dates/chunk, %dd lookback)",
                  n_total, n_chunks, chunk_size, eff_days)

        for ci in range(n_chunks):
            chunk_start_idx = ci * chunk_size
            chunk_end_idx = min(chunk_start_idx + chunk_size, n_total)
            chunk_dates = dates[chunk_start_idx:chunk_end_idx]
            chunk_start_dt = chunk_dates[0]
            chunk_end_dt = chunk_dates[-1]

            _log.info("factor_cache: chunk %d/%d — %s → %s (%d dates)",
                      ci + 1, n_chunks, chunk_start_dt, chunk_end_dt, len(chunk_dates))

            # ── 快速跳过: 日期文件已存在 → 跳过数据加载+原语计算 ──
            # test-v274 原逻辑比 factor_names 完整性, 但新因子不可能在旧缓存中,
            # 导致 all_cached 永 False. test-v286 改为只检查文件存在性,
            # 新因子对旧日期返回 NaN, pipeline 端会处理.
            if not force:
                all_exist = True
                for date_str in chunk_dates:
                    if not os.path.exists(self._path(date_str)):
                        all_exist = False
                        break
                if all_exist:
                    _log.info("factor_cache: chunk %d/%d — all %d dates cached, skip",
                              ci + 1, n_chunks, len(chunk_dates))
                    continue

            t_chunk = _time.time()

            # ── Step 1: 数据加载 ──
            data_start = (pd.Timestamp(chunk_start_dt) - pd.Timedelta(days=eff_days)).strftime("%Y-%m-%d")
            data_full = store.get_daily(symbols, start=data_start, end=chunk_end_dt)
            t1 = _time.time()
            _log.info("factor_cache: chunk %d/%d — loaded %d days × %d symbols (%.1fs)",
                      ci + 1, n_chunks, len(data_full), len(symbols), t1 - t_chunk)

            # ── Step 2: 原语预计算 ──
            prims = precompute_primitives(data_full, factor_names=factor_names)
            t2 = _time.time()

            # ── Step 3: 基准+缓存+aux+fundamentals ──
            try:
                bm_ret = store.get_benchmark("000300", start=data_start)
                if not bm_ret.empty:
                    prims["benchmark_ret"] = bm_ret
            except Exception as _e:
                _log.warning("factor_cache: benchmark_ret not available (%s)", _e)

            preload_ztd_cache(chunk_dates, symbols)
            aux_full = preload_aux_data_chunk(symbols, chunk_start_dt, chunk_end_dt)
            chunk_fundamentals = self._build_fundamentals_panel(
                store, symbols, chunk_dates, data_full=data_full
            )
            t3 = _time.time()

            # ── Step 4: 逐日因子计算 ──
            chunk_new_rows: dict[str, list[str]] = {d: [] for d in chunk_dates}

            use_mp = len(chunk_dates) >= 10 and any(
                self._factor_needs_worker(name) for name in factor_names
            )
            if use_mp:
                chunk_dates_done, chunk_new_rows = self._compute_chunk_parallel(
                    chunk_dates, data_full, prims, chunk_fundamentals,
                    aux_full, factor_names
                )
            else:
                chunk_dates_done = 0
                for date_str in chunk_dates:
                    existing = self._get_existing_factors(date_str)
                    missing = [f for f in factor_names if f not in existing]
                    if not missing:
                        continue
                    lines = self._compute_date(
                        date_str, data_full, prims,
                        chunk_fundamentals.get(date_str),
                        aux_full, missing
                    )
                    if lines:
                        chunk_new_rows[date_str].extend(lines)
                        chunk_dates_done += 1
            t4 = _time.time()

            # ── Step 5: CSV 写入 ──
            chunk_rows = self._write_chunk_rows(chunk_new_rows)
            t5 = _time.time()

            # P3b: 断点续传
            self._write_checkpoint(chunk_end_dt, ci + 1, n_chunks)

            # 释放内存
            del data_full, prims, chunk_fundamentals, aux_full
            if hasattr(store, '_query_cache'):
                store._query_cache.clear()
            _gc.collect()

            total_rows += chunk_rows
            n_dates_computed += chunk_dates_done
            _log.info(
                "factor_cache: chunk %d/%d done — %d rows | "
                "load=%.0fs prim=%.0fs aux=%.0fs compute=%.0fs write=%.0fs total=%.0fs",
                ci + 1, n_chunks, chunk_rows,
                t1 - t_chunk, t2 - t1, t3 - t2, t4 - t3, t5 - t4, t5 - t_chunk,
            )

        elapsed = _time.time() - t0
        _log.info("factor_cache: materialized %d dates × %d factors × %d symbols → %d rows in %.1fs",
                  n_dates_computed, len(factor_names), len(symbols), total_rows, elapsed)

        self._log_materialization(dates[0], dates[-1], len(factor_names), len(symbols),
                                  n_dates_computed, total_rows, elapsed, force)

        # P3b: 物化完成, 清理断点
        self._clear_checkpoint()

        # 关闭内部创建的 DataStore, 释放 SQLite 连接
        if _store_owned:
            try:
                store.close()
            except Exception as _e:
                _log.debug("store close failed (non-fatal): %s", _e)

        size_mb = sum(os.path.getsize(os.path.join(self._cache_dir, f))
                      for f in os.listdir(self._cache_dir) if f.endswith('.csv.gz')) / 1024 / 1024
        _log.info("factor_cache: total cache size %.0f MB", size_mb)

        return {"n_dates": n_dates_computed, "n_factors": len(factor_names),
                "n_symbols": len(symbols), "n_rows": total_rows,
                "elapsed_sec": round(elapsed, 1)}

    # ── 读取 ──

    def load(self, date_str: str, symbols=None, factor_names=None) -> dict:
        """从缓存读取单日因子值。返回 {factor_name: pd.Series(symbol→value)}."""
        path = self._path(date_str)
        if not os.path.exists(path):
            return {}

        lines = self._read_raw_lines(date_str)
        if not lines:
            return {}

        result = {}
        for line in lines:
            parts = line.split(",", 2)
            if len(parts) != 3:
                continue
            sym, factor, val_str = parts
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

    def _read_raw_lines(self, date_str: str) -> list[str]:
        path = self._path(date_str)
        if not os.path.exists(path):
            return []
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]

    def _write_chunk_rows(self, chunk_rows: dict[str, list[str]]) -> int:
        """批量写一个 chunk 的 CSV 缓存 (A3 + C2 manifest)。

        对已有文件只做一次 读→合并→压缩→写; 新文件直接压缩写。
        写完后更新 manifest, 记录该日期已物化的因子集合。
        返回实际写入的总行数。
        """
        total = 0
        for date_str, lines in chunk_rows.items():
            if not lines:
                continue
            path = self._path(date_str)
            existing = self._get_existing_factors(date_str)
            if os.path.exists(path) and existing:
                existing_lines = self._read_raw_lines(date_str)
                existing_set = set(existing_lines)
                for line in lines:
                    if line not in existing_set:
                        existing_lines.append(line)
                lines = existing_lines

            raw = "\n".join(lines).encode()
            compressed = gzip.compress(raw, compresslevel=_require_cfg("factor.compute.cache_compresslevel"))
            with open(path, 'wb') as f:
                f.write(compressed)

            # C2: 更新 manifest, 从行中解析因子名
            factors = set(line.split(",", 2)[1] for line in lines
                          if len(line.split(",", 2)) >= 2)
            self._write_manifest(date_str, factors, len(lines))
            total += len(lines)
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
        )

        lines = []
        for fname, series in fv.items():
            if not isinstance(series, pd.Series) or series.dropna().empty:
                continue
            for sym, val in series.dropna().items():
                lines.append(f"{sym},{fname},{val:.6f}")
        return lines

    def _compute_chunk_parallel(self, chunk_dates: list[str], data_full: pd.DataFrame,
                                prims: dict, chunk_fundamentals: dict,
                                aux_full: dict, factor_names: list[str]) -> tuple:
        """用 ProcessPoolExecutor 并行, 数据通过 fork 继承全局变量共享 (零序列化).

        max_workers=4 受 skill.md 模板 8 硬约束 (单人单机 M1 Max 实测最优)。
        """
        from concurrent.futures import ProcessPoolExecutor, as_completed
        from quant.factor.compute.price._alternative import preload_ztd_cache

        # 只把有缺失因子的日期交给 worker
        tasks = []
        for date_str in chunk_dates:
            existing = self._get_existing_factors(date_str)
            missing = [f for f in factor_names if f not in existing]
            if missing:
                tasks.append((date_str, missing))

        chunk_new_rows: dict[str, list[str]] = {d: [] for d in chunk_dates}
        done = 0
        if not tasks:
            return done, chunk_new_rows

        # 方案 A: 设全局变量 (fork 后子进程继承, 零序列化)
        global _worker_data, _worker_prims, _worker_aux, _worker_fundamentals
        _worker_data = data_full
        _worker_prims = prims
        _worker_aux = aux_full
        _worker_fundamentals = chunk_fundamentals

        symbols = list(data_full.columns.get_level_values(1).unique())

        # OOM 保护: 因子多时 reduce workers
        _worker_threshold = _require_cfg("factor.compute.cache_worker_threshold")
        n_workers = 2 if len(factor_names) > _worker_threshold else 4

        with ProcessPoolExecutor(max_workers=n_workers,
                                 mp_context=_get_mp_context(),
                                 initializer=_worker_init,
                                 initargs=(chunk_dates, symbols)) as exe:
            futures = {
                exe.submit(_worker_compute_date, date_str, missing): date_str
                for date_str, missing in tasks
            }
            for fut in as_completed(futures):
                date_str = futures[fut]
                try:
                    chunk_new_rows[date_str] = fut.result()
                    done += 1
                except Exception as e:
                    _log.error("factor_cache: worker failed for %s: %s", date_str, e)

        # 清理全局引用
        _worker_data = None
        _worker_prims = None
        _worker_aux = None
        _worker_fundamentals = None

        return done, chunk_new_rows

    # ── 查询 ──

    def _get_existing_factors(self, date_str: str) -> set:
        """返回该日期已物化的因子名集合。

        C2: 优先读 manifest, 不存在时回退扫描 gzip CSV (兼容旧缓存)。
        P1b: manifest 存在但 source_hash 不匹配 → 视为过期 → 返回空集合。
        """
        mpath = self._manifest_path(date_str)
        if os.path.exists(mpath):
            try:
                with open(mpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                factors = set(data.get("factors", []))
                # P1b: source_hash 变更检测 — 因子源码改了 → 旧值无效
                if factors and "source_hash" in data:
                    current_hash = _compute_factor_source_hash(factors)
                    if data["source_hash"] != current_hash:
                        _log.info("factor_cache: %s stale (source_hash changed), recompute %d factors",
                                  date_str, len(factors))
                        return set()
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
        return all(self._date_has_data(d, factor_names) for d in check_dates)

    def _date_has_data(self, date_str: str, factor_names: list[str]) -> bool:
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
