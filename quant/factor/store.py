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
                    force: bool = False) -> dict:
        """批量物化因子值: 预加载数据 + 原语, 逐日计算全部因子, 写入 gzip CSV。

        Args:
            date_range: 交易日列表
            factor_names: 因子名列表
            symbols: 股票池
            store: DataStore 实例
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

        # 0.3 ADR-039: 过滤到 stocks 表中的有效 symbol
        from quant.data.repos._base import DatabaseManager
        mconn = DatabaseManager.market()
        valid_syms = set(r[0] for r in mconn.execute(
            "SELECT DISTINCT symbol FROM stocks"
        ).fetchall())
        symbols_filtered = [s for s in symbols if s in valid_syms]
        _log.info("factor_cache: symbol filter %d → %d (stocks table)",
                  len(symbols), len(symbols_filtered))
        symbols = symbols_filtered
        if not symbols:
            _log.warning("factor_cache: no valid symbols after filtering, abort")
            return {"n_dates": 0, "n_factors": 0, "n_symbols": 0, "n_rows": 0,
                    "elapsed_sec": 0, "skipped": True}

        # 0.5 清理孤儿文件: 因子退役后删除无用的旧数据文件
        if not force and factor_names:
            existing_factors = set()
            for f in os.listdir(self._cache_dir):
                if f.endswith('.csv.gz'):
                    try:
                        self._scan_file_factors(f)
                        existing_factors.update(self._scan_file_factors(f))
                    except Exception:
                        pass

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

        # 2.25 加载沪深300基准收益
        try:
            bm_ret = store.get_benchmark("000300", start=full_start)
            if not bm_ret.empty:
                prims["benchmark_ret"] = bm_ret
                _log.info("factor_cache: benchmark_ret loaded (%d dates)", len(bm_ret))
        except Exception as _e:
            _log.warning("factor_cache: benchmark_ret not available (%s)", _e)

        # 2.5 预加载 ztd 缓存
        preload_ztd_cache(date_range, symbols)
        _log.info("factor_cache: ztd cache preloaded (%d dates × %d symbols)",
                  len(date_range), len(symbols))

        # 2.6 预加载 fundamentals
        _store_fundamentals = {}
        for date_str in date_range:
            _store_fundamentals[date_str] = store.get_fundamentals(symbols, date=date_str)
        _log.info("factor_cache: fundamentals ready (%d dates)", len(_store_fundamentals))

        # 3. 逐日计算 + 写入
        if force:
            for f in os.listdir(self._cache_dir):
                if f.endswith('.csv.gz'):
                    os.remove(os.path.join(self._cache_dir, f))

        total_rows = 0
        n_dates_computed = 0

        for date_str in date_range:
            # 查该日期已有因子
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
                fundamentals=_store_fundamentals.get(date_str),
                factor_names=missing,
                status_filter=None,
                factor_fail_fast=False,
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
                # 如果已有数据，追加（合并去重）
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
                total_rows += len(lines)
            n_dates_computed += 1

        elapsed = _time.time() - t0
        _log.info("factor_cache: materialized %d dates × %d factors × %d symbols → %d rows in %.1fs",
                  n_dates_computed, len(factor_names), len(symbols), total_rows, elapsed)

        self._log_materialization(start_dt, end_dt, len(factor_names), len(symbols),
                                  n_dates_computed, total_rows, elapsed, force)

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
        """检查最新日期是否覆盖了全部因子。"""
        if not date_range:
            return False
        return self._date_has_data(date_range[-1], factor_names)

    def _date_has_data(self, date_str: str, factor_names: list[str]) -> bool:
        return len(self._get_existing_factors(date_str)) >= len(factor_names)

    # ── 维护 ──

    def trim_to_max_days(self, max_days: int) -> int:
        """删除超过 max_days 天前的旧缓存文件。"""
        if max_days <= 0:
            return 0
        cutoff = (pd.Timestamp.now() - pd.Timedelta(days=max_days * 2)).strftime("%Y-%m-%d")
        deleted = 0
        for f in sorted(os.listdir(self._cache_dir)):
            if not f.endswith('.csv.gz'):
                continue
            date_str = f.replace('.csv.gz', '')
            if date_str < cutoff:
                os.remove(os.path.join(self._cache_dir, f))
                deleted += 1
        if deleted:
            _log.info("factor_cache: trimmed %d files before %s (max_days=%d)",
                      deleted, cutoff, max_days)
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
