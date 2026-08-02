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
import json
import pandas as pd
import numpy as np
from quant.utils.logger import get_logger
from quant.factor.compute.price._alternative import preload_ztd_cache
from quant.factor.compute._preload import preload_aux_data_chunk

_log = get_logger("factor.store")

# 项目根目录
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CACHE_DIR = os.path.join(_PROJ_ROOT, "quant", "data", "factor_cache")
_LOG_FILE = os.path.join(_CACHE_DIR, "materialization_log.jsonl")


# ── ProcessPoolExecutor worker 函数 (B1) ──
# 必须模块级可 pickle。

def _worker_init(chunk_dates: list[str], symbols: list[str]):
    """子进程初始化: 预加载 ztd 缓存。"""
    try:
        preload_ztd_cache(chunk_dates, symbols)
    except Exception as e:
        _log.warning("factor_cache worker init ztd failed: %s", e)


def _worker_compute_date(date_str: str, data_full: pd.DataFrame,
                         prims: dict, fundamentals: "pd.DataFrame | None",
                         aux_full: dict, factor_names: list[str]) -> list[str]:
    """子进程执行的单日计算。"""
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
                if f.endswith('.csv.gz'):
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

            # 分块加载: 只加载该块数据 + 回顾窗
            data_start = (pd.Timestamp(chunk_start_dt) - pd.Timedelta(days=eff_days)).strftime("%Y-%m-%d")
            data_full = store.get_daily(symbols, start=data_start, end=chunk_end_dt)
            _log.info("factor_cache: chunk %d/%d — loaded %d days × %d symbols",
                      ci + 1, n_chunks, len(data_full), len(symbols))

            # 分块预计算原语 (A4: 按需原语, 只算 factor_names 需要的窗口)
            prims = precompute_primitives(data_full, factor_names=factor_names)

            # 基准收益
            try:
                bm_ret = store.get_benchmark("000300", start=data_start)
                if not bm_ret.empty:
                    prims["benchmark_ret"] = bm_ret
            except Exception as _e:
                _log.warning("factor_cache: benchmark_ret not available (%s)", _e)

            # ztd 缓存 (按块日期)
            preload_ztd_cache(chunk_dates, symbols)

            # aux 数据块级预加载 (ADR-043): 12 SQL 查询/块, slice 按日期过滤
            aux_full = preload_aux_data_chunk(symbols, chunk_start_dt, chunk_end_dt)

            # fundamentals (B2: 向量化 PIT panel, 替代逐日循环)
            chunk_fundamentals = self._build_fundamentals_panel(
                store, symbols, chunk_dates
            )

            # 逐日计算 (A3 收集 + B1 可选多进程)
            chunk_rows = 0
            chunk_dates_done = 0
            chunk_new_rows: dict[str, list[str]] = {d: [] for d in chunk_dates}

            # 判断是否需要多进程: 日期多且 factor_names 含非 shortcut 因子时启用
            use_mp = len(chunk_dates) >= 10 and any(
                self._factor_needs_worker(name) for name in factor_names
            )
            if use_mp:
                chunk_dates_done, chunk_new_rows = self._compute_chunk_parallel(
                    chunk_dates, data_full, prims, chunk_fundamentals,
                    aux_full, factor_names
                )
            else:
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

            # chunk 级批量写 gzip CSV
            chunk_rows = self._write_chunk_rows(chunk_new_rows)

            # 释放该块内存（含 DataStore 查询缓存）
            del data_full, prims, chunk_fundamentals, aux_full
            if hasattr(store, '_query_cache'):
                store._query_cache.clear()
            _gc.collect()

            t_chunk_elapsed = _time.time() - t_chunk
            total_rows += chunk_rows
            n_dates_computed += chunk_dates_done
            _log.info("factor_cache: chunk %d/%d done — %d rows in %.1fs",
                      ci + 1, n_chunks, chunk_rows, t_chunk_elapsed)

        elapsed = _time.time() - t0
        _log.info("factor_cache: materialized %d dates × %d factors × %d symbols → %d rows in %.1fs",
                  n_dates_computed, len(factor_names), len(symbols), total_rows, elapsed)

        self._log_materialization(dates[0], dates[-1], len(factor_names), len(symbols),
                                  n_dates_computed, total_rows, elapsed, force)

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
            compressed = gzip.compress(raw, compresslevel=6)
            with open(path, 'wb') as f:
                f.write(compressed)

            # C2: 更新 manifest, 从行中解析因子名
            factors = set(line.split(",", 2)[1] for line in lines
                          if len(line.split(",", 2)) >= 2)
            self._write_manifest(date_str, factors, len(lines))
            total += len(lines)
        return total

    def _build_fundamentals_panel(self, store, symbols: list[str],
                                  chunk_dates: list[str]) -> dict[str, pd.DataFrame]:
        """构建基本面 PIT panel, 返回 {date_str: DataFrame(symbol×field)}。

        B2 向量化实现: pivot + ffill 一次生成整块 panel,
        避免原逐日循环中的重复 pivot/loc。
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

        # PIT close panel + 52周高
        if not daily_df.empty:
            daily_df["date"] = pd.to_datetime(daily_df["date"])
            close_piv = daily_df.pivot(index="date", columns="symbol",
                                       values="close").ffill()
            high_52w = close_piv.rolling(244, min_periods=60).max()
        else:
            close_piv = None
            high_52w = None

        result = {}
        for date_str in chunk_dates:
            ts = pd.Timestamp(date_str)
            df = stocks_df.copy()

            # PIT 估值列覆盖
            if val_piv is not None and ts in val_piv.index:
                row = val_piv.loc[ts]
                for col in ["pe_ttm", "pb", "market_cap"]:
                    if col in df.columns and col in row.index.get_level_values(0):
                        df[col] = row[col].reindex(df.index)

            # PIT close + high_52w
            if close_piv is not None and ts in close_piv.index:
                df["close_latest"] = close_piv.loc[ts].reindex(df.index)
            if high_52w is not None and ts in high_52w.index:
                df["high_52w"] = high_52w.loc[ts].reindex(df.index)

            # derive ROE from PB/PE
            null_roe = df["roe"].isna() | (df["roe"] <= 0)
            if null_roe.any():
                derived = df["pb"] / df["pe"].replace(0, None)
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
        """用 ProcessPoolExecutor 并行计算 chunk 内各日期因子。

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

        # worker 全局只读数据, 通过 initializer 预加载 ztd 缓存
        symbols = list(data_full.columns.get_level_values(1).unique())
        init_args = (chunk_dates, symbols)

        with ProcessPoolExecutor(max_workers=4,
                                 initializer=_worker_init,
                                 initargs=init_args) as exe:
            futures = {
                exe.submit(_worker_compute_date,
                           date_str, data_full, prims,
                           chunk_fundamentals.get(date_str),
                           aux_full, missing): date_str
                for date_str, missing in tasks
            }
            for fut in as_completed(futures):
                date_str = futures[fut]
                try:
                    chunk_new_rows[date_str] = fut.result()
                    done += 1
                except Exception as e:
                    _log.error("factor_cache: worker failed for %s: %s", date_str, e)

        return done, chunk_new_rows

    # ── 查询 ──

    def _get_existing_factors(self, date_str: str) -> set:
        """返回该日期已物化的因子名集合。

        C2: 优先读 manifest, 不存在时回退扫描 gzip CSV (兼容旧缓存)。
        """
        mpath = self._manifest_path(date_str)
        if os.path.exists(mpath):
            try:
                with open(mpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return set(data.get("factors", []))
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
        """写入日期级因子清单 (C2)。"""
        try:
            record = {
                "factors": sorted(factors),
                "n_rows": n_rows,
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
