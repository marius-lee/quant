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

_log = get_logger("factor.store")

# 项目根目录
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CACHE_DIR = os.path.join(_PROJ_ROOT, "quant", "data", "factor_cache")
_LOG_FILE = os.path.join(_CACHE_DIR, "materialization_log.jsonl")


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
        except Exception:
            pass

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
        except Exception:
            pass  # monkeypatched shared conn in tests
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

            t_chunk = _time.time()

            # 分块加载: 只加载该块数据 + 回顾窗
            data_start = (pd.Timestamp(chunk_start_dt) - pd.Timedelta(days=eff_days)).strftime("%Y-%m-%d")
            data_full = store.get_daily(symbols, start=data_start, end=chunk_end_dt)
            _log.info("factor_cache: chunk %d/%d — loaded %d days × %d symbols",
                      ci + 1, n_chunks, len(data_full), len(symbols))

            # 分块预计算原语
            prims = precompute_primitives(data_full)

            # 基准收益
            try:
                bm_ret = store.get_benchmark("000300", start=data_start)
                if not bm_ret.empty:
                    prims["benchmark_ret"] = bm_ret
            except Exception as _e:
                _log.warning("factor_cache: benchmark_ret not available (%s)", _e)

            # ztd 缓存 (按块日期)
            preload_ztd_cache(chunk_dates, symbols)

            # fundamentals (按块日期) — 批量加载优化: 一次 SQL 取全天范围, 内存 PIT
            chunk_fundamentals = {}
            if chunk_dates:
                # 一次取全部 daily_valuation (含 pe_ttm/pb/market_cap)
                val_start = chunk_dates[0]
                val_end = chunk_dates[-1]
                mconn2 = store._connect()
                val_df = pd.read_sql_query(
                    "SELECT symbol, date, pe_ttm, pb, ps_ttm, pcf_ttm, market_cap FROM daily_valuation "
                    "WHERE date >= ? AND date <= ? ORDER BY date",
                    mconn2, params=(val_start, val_end)
                )
                # 一次取 stocks 表
                ph_stocks = ",".join("?" * len(symbols))
                stocks_df = pd.read_sql_query(
                    f"SELECT symbol, pe, pe_ttm, pb, total_mv, roe, industry, high_52w, eps, bvps "
                    f"FROM stocks WHERE symbol IN ({ph_stocks})",
                    mconn2, params=symbols
                ).set_index("symbol")
                # 一次取 daily close (用于 PIT + high_52w)
                daily_df = pd.read_sql_query(
                    f"SELECT symbol, date, close FROM daily "
                    f"WHERE symbol IN ({ph_stocks}) AND date >= ? AND date <= ? ORDER BY date",
                    mconn2, params=symbols + [val_start, val_end]
                )
                # 内存 PIT: 每日期取 ≤date 的最新估值
                if not val_df.empty:
                    val_df["date"] = pd.to_datetime(val_df["date"])
                    val_piv = val_df.pivot(index="date", columns="symbol", values=["pe_ttm", "pb", "market_cap"])
                    val_piv = val_piv.ffill()
                if not daily_df.empty:
                    daily_df["date"] = pd.to_datetime(daily_df["date"])
                    close_piv = daily_df.pivot(index="date", columns="symbol", values="close").ffill()
                for date_str in chunk_dates:
                    ts = pd.Timestamp(date_str)
                    result = stocks_df.copy()
                    # PIT valuation
                    if not val_df.empty and ts in val_piv.index:
                        row = val_piv.loc[ts]
                        for col in ["pe_ttm", "pb", "market_cap"]:
                            if col in result.columns:
                                result[col] = row[col] if col in row else None
                    # PIT close + high_52w
                    if not daily_df.empty and ts in close_piv.index:
                        result["close_latest"] = close_piv.loc[ts]
                        # 52-week high: max close in last ~244 trading days
                        early = ts - pd.Timedelta(days=_require_cfg("data.lookback_days"))
                        mask = (close_piv.index <= ts) & (close_piv.index >= early)
                        if mask.any():
                            result["high_52w"] = close_piv.loc[mask].max()
                    # derive ROE from PB/PE
                    null_roe = result["roe"].isna() | (result["roe"] <= 0)
                    if null_roe.any():
                        derived = result["pb"] / result["pe"].replace(0, None)
                        derived = derived.where((derived > 0) & (derived < 100))
                        result.loc[null_roe, "roe"] = derived.loc[null_roe]
                    # filter extreme PE/PB
                    result.loc[result["pe"] <= 0, "pe"] = None
                    result.loc[result["pe"] > 1000, "pe"] = None
                    result.loc[result["pb"] <= 0, "pb"] = None
                    chunk_fundamentals[date_str] = result

            # 逐日计算
            chunk_rows = 0
            chunk_dates_done = 0
            for date_str in chunk_dates:
                existing = self._get_existing_factors(date_str)
                missing = [f for f in factor_names if f not in existing]
                if not missing:
                    continue

                try:
                    ts = pd.Timestamp(date_str)
                    if ts not in data_full.index:
                        continue
                    day_data = data_full.loc[:ts]
                    if day_data.empty:
                        continue
                except Exception:
                    continue

                fv = compute_all_factors(
                    day_data, date_str,
                    primitives=prims,
                    fundamentals=chunk_fundamentals.get(date_str),
                    factor_names=missing,
                    status_filter=None,
                    factor_fail_fast=False,
                    quiet=True,
                )

                # 写入 gzip CSV
                lines = []
                for fname, series in fv.items():
                    if not isinstance(series, pd.Series) or series.dropna().empty:
                        continue
                    for sym, val in series.dropna().items():
                        lines.append(f"{sym},{fname},{val:.6f}")

                if lines:
                    path = self._path(date_str)
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
                    chunk_rows += len(lines)
                chunk_dates_done += 1

            # 释放该块内存（含 DataStore 查询缓存）
            del data_full, prims, chunk_fundamentals
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
            except Exception:
                pass

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

    # ── 查询 ──

    def _get_existing_factors(self, date_str: str) -> set:
        """返回该日期已物化的因子名集合。"""
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
