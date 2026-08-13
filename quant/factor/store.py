"""因子缓存存储 v470 — parquet 列式分区 + fork 共享内存 + checkpoint 续传。

设计原则:
  - 存储布局: factor_cache/parquet_f/{factor}/{year}.parquet (因子×年分区)
  - 列: date_i16 (全局交易日序号), symbol_i16 (字典 idx), value_f32 (float32)
  - 压缩: zstd level 3 — 相比 v469 按日期分区, 减少文件数量 (1886 → ~300 文件)
  - 多进程: fork 模式, 一次性继承 data_full/prims/aux/fundamentals (COW),
    单 Worker 顺序处理日期范围, 内存可控 (<3GB/worker)
  - checkpoint: 记录 last_date + failed_dates, resume 时加回重试
  - manifest: 每日期×因子 source_hash, 细粒度失效
  - 零 fallback: 读失败即抛, 无旧格式回退

对标: Qlib 因子库(parquet) + DolphinDB factorDB
"""
import os
import json
import hashlib
import inspect
import shutil
import gc
import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger
from quant.factor.compute._dispatch import compute_all_factors
from quant.factor.compute._primitives import precompute_primitives
from quant.factor.compute._preload import preload_aux_data_chunk, slice_aux_for_date
from quant.factor.compute.price._alternative import preload_ztd_cache, clear_ztd_cache
from quant.factor.windows import max_factor_calendar_days

_log = get_logger("quant.factor.store")

_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CACHE_DIR = os.path.join(_PROJ_ROOT, "quant", "data", "factor_cache")
_PARQUET_DIR = os.path.join(_CACHE_DIR, "parquet_f")

# 全局共享数据 (fork COW 继承)
_DATA_FULL = None
_PRIMS = None
_AUX_FULL = None
_FUNDAMENTALS = None
_SYMBOLS = None

# per-factor 源码 hash 缓存 (代码不变则 hash 不变, 进程内安全缓存)
_SOURCE_HASH_CACHE: dict[str, str] = {}


def _source_hash_single(factor_name: str) -> str:
    """单因子源码 hash (缓存) — meta 与缺失判定统一用此口径。"""
    if factor_name in _SOURCE_HASH_CACHE:
        return _SOURCE_HASH_CACHE[factor_name]
    h = _compute_factor_source_hash({factor_name})
    _SOURCE_HASH_CACHE[factor_name] = h
    return h


def _compute_factor_source_hash(factor_names: set[str]) -> str:
    """计算因子函数源码复合 hash — 检测函数变更触发重算。"""
    h = hashlib.sha256()
    try:
        h.update(inspect.getsource(precompute_primitives).encode())
    except (OSError, TypeError):
        h.update(b"precompute_primitives")
    for name in sorted(factor_names):
        fn = None
        try:
            from quant.factor.compute.price import _PRICE_FN_MAP
            from quant.factor.compute.fundamental import _FUNDAMENTAL_FN_MAP
            if name in _PRICE_FN_MAP:
                fn = _PRICE_FN_MAP[name][0]
            elif name in _FUNDAMENTAL_FN_MAP:
                fn = _FUNDAMENTAL_FN_MAP[name][1]
        except Exception:
            pass
        if fn is not None:
            try:
                h.update(inspect.getsource(fn).encode())
            except (OSError, TypeError):
                h.update(name.encode())
        try:
            from quant.factor.compute._primitives import FACTOR_SHORTCUT
            sc = FACTOR_SHORTCUT.get(name)
            if sc is not None:
                h.update(inspect.getsource(sc).encode())
        except (OSError, TypeError):
            h.update(("sc:" + name).encode())
    return h.hexdigest()[:16]


class FactorStore:
    """因子缓存存储 v470 — parquet 列式分区 + fork 共享内存。

    流程:
      1. store.materialize(dates, factor_names, symbols, store) → 批量计算并写入
      2. store.load(date, symbols, factor_names) → {factor_name: Series}
      3. store.is_materialized(date_range, factor_names) → bool
    """

    def __init__(self, cache_dir: str = None, db_path: str = None):
        """初始化因子缓存存储。

        Args:
            cache_dir: 缓存目录路径 (parquet_f 子目录)
            db_path: 向后兼容旧 SQLite 路径 — 用其父目录 + /factor_cache/
        """
        if cache_dir is not None:
            self._cache_dir = cache_dir
        elif db_path is not None:
            parent = os.path.dirname(db_path)
            self._cache_dir = os.path.join(parent, "factor_cache")
        else:
            self._cache_dir = _CACHE_DIR
        self._parquet_dir = os.path.join(self._cache_dir, "parquet_f")
        os.makedirs(self._parquet_dir, exist_ok=True)

    # ── 元数据 ──

    def _metadata_path(self, name: str) -> str:
        return os.path.join(self._cache_dir, "metadata", f"{name}.json")

    def _load_json(self, path: str) -> dict:
        if not os.path.exists(path):
            return {}
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _save_json(self, path: str, data: dict):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    def _load_symbol_map(self) -> dict[str, int]:
        return self._load_json(self._metadata_path("symbol_dict"))

    def _save_symbol_map(self, mapping: dict[str, int]):
        self._save_json(self._metadata_path("symbol_dict"), mapping)

    def _load_trading_days(self) -> list[str]:
        data = self._load_json(self._metadata_path("trading_days"))
        return data.get("dates", [])

    def _save_trading_days(self, dates: list[str]):
        self._save_json(self._metadata_path("trading_days"), {"dates": dates})

    def _load_factor_meta(self, factor_name: str) -> dict:
        return self._load_json(self._metadata_path(f"factor_{factor_name}"))

    def _save_factor_meta(self, factor_name: str, meta: dict):
        self._save_json(self._metadata_path(f"factor_{factor_name}"), meta)

    def _build_symbol_map(self, symbols: list[str]) -> dict[str, int]:
        return {s: i for i, s in enumerate(sorted(set(symbols)))}

    def _parquet_path(self, factor_name: str, year: int) -> str:
        return os.path.join(self._parquet_dir, factor_name, f"{year}.parquet")

    @property
    def _checkpoint_path(self) -> str:
        return os.path.join(self._cache_dir, "_checkpoint.json")

    @property
    def _log_file(self) -> str:
        return os.path.join(self._cache_dir, "materialization_log.jsonl")

    def close(self):
        pass

    # ── Worker (fork COW) ──

    @staticmethod
    def _worker_main(start_idx: int, end_idx: int, factor_names: list[str],
                     date_list: list[str], symbol_map: dict[str, int],
                     date_to_idx: dict[str, int],
                     source_hash: str) -> dict:
        """Worker 计算日期范围内的所有因子, 返回 {factor: {date_i16: [(symbol_i16, value_f32)]}}。"""
        import time as _time
        t0 = _time.time()
        global _DATA_FULL, _PRIMS, _AUX_FULL, _FUNDAMENTALS, _SYMBOLS

        if _DATA_FULL is None:
            raise RuntimeError("Worker: _DATA_FULL is None — fork failed")

        results: dict[str, dict[int, list[tuple[int, np.float32]]]] = {}
        for fname in factor_names:
            results[fname] = {}

        failed_dates: list[str] = []

        for i in range(start_idx, end_idx):
            date_str = date_list[i]
            try:
                date_idx = date_to_idx[date_str]
                d_idx = np.int16(date_idx)

                aux_sliced = slice_aux_for_date(_AUX_FULL, date_str) if _AUX_FULL else {}
                fund = _FUNDAMENTALS.get(date_str) if _FUNDAMENTALS else None

                fv = compute_all_factors(
                    _DATA_FULL, date_str,
                    primitives=_PRIMS,
                    fundamentals=fund,
                    preloaded_aux_chunk=aux_sliced,
                    factor_names=factor_names,
                    status_filter=None,
                    factor_fail_fast=False,
                    quiet=True,
                    financials_cache={},
                )

                for fname, series in fv.items():
                    if not isinstance(series, pd.Series) or series.dropna().empty:
                        continue
                    rows = []
                    for sym, val in series.dropna().items():
                        if sym in symbol_map:
                            rows.append((np.int16(symbol_map[sym]), np.float32(val)))
                    if rows:
                        results[fname][d_idx] = rows

            except Exception as e:
                import traceback
                _log.error("Worker date %s failed for factor batch: %s", date_str, traceback.format_exc())
                failed_dates.append(date_str)

        _log.info("Worker %d-%d: computed %d dates in %.1fs (failed=%d)",
                  start_idx, end_idx, end_idx - start_idx, _time.time() - t0, len(failed_dates))
        return {"results": results, "failed_dates": failed_dates, "source_hash": source_hash}

    def materialize(self,
                    date_range: list[str],
                    factor_names: list[str],
                    symbols: list[str],
                    store=None,
                    force: bool = False,
                    chunk_days: int = 200,
                    workers: int = None) -> dict:
        """批量物化因子值: fork pool + parquet column 分区写入。

        Args:
            date_range: 交易日列表
            factor_names: 因子名列表
            symbols: 股票列表
            store: DataStore 实例
            force: True 时删除旧数据重新物化
            chunk_days: 每块最大交易日数
            workers: 并行 worker 数 (默认 3-4)

        Returns:
            dict: {n_dates, n_factors, n_symbols, n_rows, elapsed_sec}
        """
        import time as _time
        from quant.data.store import DataStore

        if workers is None:
            workers = min(mp.cpu_count(), 4)
            workers = max(2, workers)

        _store_owned = store is None
        if store is None:
            store = DataStore()

        t0 = _time.time()

        # 0.0 合并上次中断残留的 part 文件 (幂等, 崩溃续跑安全)
        _pre_merged = self._merge_pending_parts()
        if _pre_merged:
            _log.info("factor_cache: merged %d stale part groups from previous run", _pre_merged)

        # 0.1 过滤到有效 symbol
        from quant.data.repos._base import DatabaseManager
        mconn = DatabaseManager.market()
        valid_syms = set(r[0] for r in mconn.execute(
            "SELECT DISTINCT symbol FROM stocks"
        ).fetchall())
        try:
            mconn.close()
        except Exception:
            pass
        symbols = [s for s in symbols if s in valid_syms]
        if not symbols:
            _log.warning("factor_cache: no valid symbols after filtering, abort")
            return {"n_dates": 0, "n_factors": 0, "n_symbols": 0, "n_rows": 0,
                    "elapsed_sec": 0, "skipped": True, "failed_dates": []}

        _log.info("factor_cache: %d symbols, %d factors, %d dates", len(symbols), len(factor_names), len(date_range))

        # 0.2 建立交易日序号 (全局, 持续累积)
        all_cached = self._load_trading_days()
        existing_dates = set(all_cached)
        new_dates = [d for d in date_range if d not in existing_dates]
        if new_dates:
            all_cached.extend(sorted(new_dates))
            all_cached = sorted(set(all_cached))
            self._save_trading_days(all_cached)
        date_to_idx = {d: i for i, d in enumerate(all_cached)}

        # 0.3 建立 symbol 字典 (持续累积, i16 idx)
        sym_map = self._load_symbol_map()
        new_syms = {s for s in symbols if s not in sym_map}
        if new_syms:
            max_idx = max(sym_map.values()) + 1 if sym_map else 0
            for s in sorted(new_syms):
                sym_map[s] = max_idx
                max_idx += 1
            self._save_symbol_map(sym_map)

        # 0.4 确定待算 (日期, 缺失因子)
        source_hash = _compute_factor_source_hash(set(factor_names))
        todo: list[str] = []
        for date_str in sorted(date_range):
            if force:
                todo.append(date_str)
                continue
            missing = self._date_missing_factors(date_str, factor_names, source_hash)
            if missing:
                todo.append(date_str)

        if not todo:
            _log.info("factor_cache: all dates already fully materialized, skip")
            return {"n_dates": len(date_range), "n_factors": len(factor_names),
                    "n_symbols": len(symbols), "n_rows": 0, "elapsed_sec": 0,
                    "skipped": True, "failed_dates": []}

        # 0.5 checkpoint resume
        _ckpt = self._read_checkpoint()
        if _ckpt and _ckpt.get("last_date") and not force and _ckpt.get("source_hash") == source_hash:
            _resume_after = _ckpt["last_date"]
            _resume_retry = set(_ckpt.get("failed_dates") or [])
            todo = [d for d in todo if d > _resume_after or d in _resume_retry]
            _log.info("factor_cache: checkpoint resume — %d pending dates after %s (%d failed retry)",
                      len(todo), _resume_after, len(_resume_retry))
            if not todo:
                self._clear_checkpoint()
                return {"n_dates": 0, "n_factors": len(factor_names),
                        "n_symbols": len(symbols), "n_rows": 0, "elapsed_sec": 0,
                        "skipped": True, "failed_dates": []}

        # 0.6 准备共享数据 (按 chunk 分块装载 — 控制内存, M1 8GB 硬约束)
        eff_days = max(_require_cfg("data.lookback_days"), max_factor_calendar_days(factor_names))
        date_list = todo

        _log.info("factor_cache: %d pending dates → %d workers × %dd lookback",
                  len(date_list), workers, eff_days)

        total_rows = 0
        n_dates_computed = 0
        failed_dates: list[str] = []
        per_factor_dates: dict[str, set] = {}

        n_chunks = max(1, (len(date_list) + chunk_days - 1) // chunk_days)
        ctx = mp.get_context('fork')
        idx_to_date = {i: d for d, i in date_to_idx.items()}
        part_seq = 0  # 全局递增 part 序号 — 同 chunk 内多 slice 各自独立 part, 避免覆盖

        for ci in range(n_chunks):
            s = ci * chunk_days
            e = min((ci + 1) * chunk_days, len(date_list))
            if s >= len(date_list):
                continue
            chunk_dates = date_list[s:e]
            _log.info("factor_cache: chunk %d/%d — %s .. %s (%d dates)",
                      ci + 1, n_chunks, chunk_dates[0], chunk_dates[-1], len(chunk_dates))

            # 本块最早需数据起始日期 (含 lookback)
            earliest_date = chunk_dates[0]
            data_start = (pd.Timestamp(earliest_date) - pd.Timedelta(days=eff_days)).strftime("%Y-%m-%d")
            latest_date = chunk_dates[-1]

            data_full = store.get_daily(symbols, start=data_start, end=latest_date)
            _log.info("factor_cache: loaded %d days × %d symbols (%.1fs)",
                      len(data_full), len(symbols), _time.time() - t0)

            prims = precompute_primitives(data_full, factor_names=factor_names)
            _log.info("factor_cache: primitives computed (%.1fs)", _time.time() - t0)

            try:
                bm_ret = store.get_benchmark("000300", start=data_start)
                if not bm_ret.empty:
                    prims["benchmark_ret"] = bm_ret
            except Exception as _e:
                _log.warning("factor_cache: benchmark_ret not available (%s)", _e)

            preload_ztd_cache(chunk_dates, symbols)

            aux_full = preload_aux_data_chunk(symbols, earliest_date, latest_date)
            _log.info("factor_cache: aux data loaded (%.1fs)", _time.time() - t0)

            _fund_shards = self._build_fundamentals_panel(store, symbols, chunk_dates, data_full=data_full)
            _log.info("factor_cache: fundamentals panel built (%.1fs)", _time.time() - t0)

            # 设置全局共享数据 (fork COW 继承给 workers)
            global _DATA_FULL, _PRIMS, _AUX_FULL, _FUNDAMENTALS, _SYMBOLS
            _DATA_FULL = data_full
            _PRIMS = prims
            _AUX_FULL = aux_full
            _FUNDAMENTALS = _fund_shards
            _SYMBOLS = symbols

            # 本块内按日期分片分配给 workers (每个 worker 连续日期段)
            dates_in_chunk = chunk_dates
            n_worker_slices = min(workers, len(dates_in_chunk))
            slice_size = -(-len(dates_in_chunk) // n_worker_slices)  # ceil 除, 覆盖全部日期
            slices = []
            for wi in range(n_worker_slices):
                _ws = wi * slice_size
                _we = min((wi + 1) * slice_size, len(dates_in_chunk))
                if _ws < _we:
                    slices.append((s + _ws, s + _we, wi, n_worker_slices))

            with ctx.Pool(processes=workers) as pool:
                async_results = []
                for _ws, _we, wi, total_slices in slices:
                    _log.info("factor_cache: slice %d/%d (dates %s → %s, %d dates)",
                              wi + 1, total_slices, date_list[_ws], date_list[_we - 1], _we - _ws)
                    async_results.append(pool.apply_async(
                        self._worker_main,
                        (_ws, _we, factor_names, date_list, sym_map, date_to_idx, source_hash)
                    ))

                chunk_results = [ar.get() for ar in async_results]

            for chunk_result in chunk_results:
                for fname, date_rows in chunk_result["results"].items():
                    if not date_rows:
                        continue
                    # 按 (factor, year) 分组待写 — rows: (date_i16, symbol_i16, value_f32)
                    year_rows: dict[int, list[tuple[int, int, float]]] = {}
                    covered_dates: list[str] = []
                    for d_idx, rows in date_rows.items():
                        yr = int(idx_to_date[d_idx][:4])
                        year_rows.setdefault(yr, []).extend(
                            (d_idx, sym_idx, val) for sym_idx, val in rows)
                        covered_dates.append(idx_to_date[d_idx])
                    for yr, rows in year_rows.items():
                        self._write_factor_date_part(fname, yr, part_seq, rows)
                        part_seq += 1
                    self._update_factor_meta(fname, covered_dates,
                                              source_hash, set(factor_names))
                    per_factor_dates.setdefault(fname, set()).update(date_rows.keys())
                inc_rows = sum(len(r) for rr in chunk_result["results"].values() for r in rr.values())
                total_rows += inc_rows
                _covered = set()
                for _rr in chunk_result["results"].values():
                    _covered.update(_rr.keys())
                n_dates_computed += len(_covered)
                failed_dates.extend(chunk_result.get("failed_dates", []))

            self._write_checkpoint(chunk_dates[-1], ci + 1, n_chunks, list(failed_dates), source_hash)

            # 释放 chunk 内存 (为下块腾空间)
            _DATA_FULL = _PRIMS = _AUX_FULL = _FUNDAMENTALS = _SYMBOLS = None
            try:
                del data_full, prims, aux_full, _fund_shards
            except Exception:
                pass
            gc.collect()

        elapsed = _time.time() - t0

        if failed_dates:
            pass
        else:
            self._clear_checkpoint()

        # 末尾合并本次全部 part → 主文件 (解锁后执行, 复用 load/trim 路径)
        _merged = self._merge_pending_parts()
        if _merged:
            _log.info("factor_cache: merged %d part groups to main parquet", _merged)

        # 关闭内部创建的 DataStore
        if _store_owned:
            try:
                store.close()
            except Exception:
                pass

        clear_ztd_cache()
        gc.collect()

        _log.info("factor_cache: materialized %d dates × %d factors → %d rows in %.1fs (workers=%d)",
                  n_dates_computed, len(factor_names), total_rows, elapsed, workers)
        if per_factor_dates:
            _log.info("factor_cache: per-factor dates covered: %s",
                      ", ".join(f"{k}={len(v)}" for k, v in sorted(per_factor_dates.items())))
        _zero_coverage = [n for n in factor_names
                          if n not in per_factor_dates or not per_factor_dates[n]]
        if _zero_coverage:
            _log.error("factor_cache: %d factors produced ZERO dates this run: %s",
                       len(_zero_coverage), ",".join(_zero_coverage))
        self._log_materialization(date_list[0], date_list[-1], len(factor_names), len(symbols),
                                  n_dates_computed, total_rows, elapsed, force)

        if failed_dates:
            _log.error("factor_cache: %d dates FAILED: %s", len(failed_dates),
                       ",".join(sorted(failed_dates)[:50]))

        return {"n_dates": n_dates_computed, "n_factors": len(factor_names),
                "n_symbols": len(symbols), "n_rows": total_rows,
                "elapsed_sec": round(elapsed, 1), "failed_dates": failed_dates}

    def _part_path(self, factor_name: str, year: int, part_id: int) -> str:
        """part 文件路径: {factor}/{year}.part{part_id} (无 .parquet 后缀,
        避免 trim_to_max_days/bulk_load 的 endswith('.parquet') 扫描误判)。"""
        return os.path.join(self._parquet_dir, factor_name, f"{year}.part{part_id}")

    def _write_factor_date_part(self, factor_name: str, year: int,
                                part_id: int,
                                rows: list[tuple[int, int, float]]) -> None:
        """写本 chunk 的 part 文件 (纯新增, 不读旧文件)。

        rows: [(date_i16, symbol_i16, value_f32), ...]
        每 (factor, year, chunk) 一个独立 part; 全程结束后 _merge_pending_parts
        合并到主文件 {year}.parquet — 消除旧实现的整年度 read-modify-write 放大。
        """
        if not rows:
            return
        ppath = self._part_path(factor_name, year, part_id)
        os.makedirs(os.path.dirname(ppath), exist_ok=True)

        new_records = [{
            'date_i16': np.int16(d_idx),
            'symbol_i16': np.int16(sym_idx),
            'value_f32': np.float32(val),
        } for d_idx, sym_idx, val in rows]
        new_df = pd.DataFrame(new_records)
        new_df['date_i16'] = new_df['date_i16'].astype('int16')
        new_df['symbol_i16'] = new_df['symbol_i16'].astype('int16')
        new_df['value_f32'] = new_df['value_f32'].astype('float32')
        new_df.to_parquet(ppath, compression='zstd', compression_level=3, index=False)

    def _merge_pending_parts(self) -> int:
        """合并全部残留 part 文件到主 parquet (幂等, 可重入)。

        崩溃残留: materialize 开头调用本方法合并上次中断的 part;
        正常结束: 末尾调用合并本次 part。零 fallback — 主文件损坏按空重建
        (缓存自愈, 同旧 _write_factor_date_rows 行为)。
        """
        merged = 0
        while True:
            parts: list[tuple[str, int, int, str]] = []  # (factor, year, part_id, path)
            for fname in sorted(os.listdir(self._parquet_dir)):
                fdir = os.path.join(self._parquet_dir, fname)
                if not os.path.isdir(fdir):
                    continue
                for f in sorted(os.listdir(fdir)):
                    if ".part" not in f or f.endswith(".parquet"):
                        continue
                    try:
                        base, _, pid_str = f.rpartition(".part")
                        year = int(base)
                        part_id = int(pid_str)
                    except ValueError:
                        continue
                    parts.append((fname, year, part_id, os.path.join(fdir, f)))
            if not parts:
                break
            by_key: dict[tuple[str, int], list[str]] = {}
            for fname, year, _pid, p in parts:
                by_key.setdefault((fname, year), []).append(p)
            for (fname, year), paths in by_key.items():
                ppath = self._parquet_path(fname, year)
                os.makedirs(os.path.dirname(ppath), exist_ok=True)
                existing = None
                if os.path.exists(ppath):
                    try:
                        existing = pd.read_parquet(ppath)
                    except Exception as _e:
                        _log.warning("factor_cache: %s corrupt, rebuilding: %s", ppath, _e)
                        existing = None
                frames = [existing] if existing is not None and len(existing) else []
                for p in sorted(paths):
                    frames.append(pd.read_parquet(p))
                combined = pd.concat(frames, ignore_index=True)
                combined = combined.drop_duplicates(
                    subset=['date_i16', 'symbol_i16'], keep='last')
                combined.to_parquet(ppath, compression='zstd',
                                    compression_level=3, index=False)
                for p in paths:
                    try:
                        os.remove(p)
                    except OSError as _e:
                        _log.warning("factor_cache: part remove failed (%s): %s", p, _e)
                merged += 1
        return merged

    def _update_factor_meta(self, factor_name: str, covered_dates: list[str],
                            source_hash: str, all_factors: set[str]) -> None:
        """更新因子 meta: source_hash (per-factor 口径) + 已物化日期集合。"""
        meta = self._load_factor_meta(factor_name)
        if not meta:
            meta = {"source_hash": None, "dates": []}
        meta["source_hash"] = _source_hash_single(factor_name)
        existing = set(meta.get("dates", []))
        existing.update(covered_dates)
        meta["dates"] = sorted(existing)
        meta["first_date"] = meta["dates"][0] if meta["dates"] else None
        meta["last_date"] = meta["dates"][-1] if meta["dates"] else None
        self._save_factor_meta(factor_name, meta)

    def load(self, date_str: str, symbols=None, factor_names=None) -> dict:
        """从缓存读取单日因子值。返回 {factor_name: Series(symbol→value)}。"""
        trading_days = self._load_trading_days()
        if date_str not in trading_days:
            return {}
        date_idx = trading_days.index(date_str)
        year = int(date_str[:4])

        sym_map = self._load_symbol_map()
        idx_to_sym = {v: k for k, v in sym_map.items()}

        result = {}
        # 遍历 parquet_f/{factor}/{year}.parquet
        for fname in os.listdir(self._parquet_dir):
            if factor_names and fname not in factor_names:
                continue
            fdir = os.path.join(self._parquet_dir, fname)
            if not os.path.isdir(fdir):
                continue
            ppath = os.path.join(fdir, f"{year}.parquet")
            if not os.path.exists(ppath):
                continue
            df = pd.read_parquet(ppath, filters=[('date_i16', '=', date_idx)])
            if df.empty:
                continue
            series = pd.Series(
                df['value_f32'].values,
                index=[idx_to_sym.get(int(s), str(s)) for s in df['symbol_i16'].values],
                name=fname,
            )
            if symbols:
                series = series.reindex(symbols)
            result[fname] = series
        return result

    def bulk_load(self, dates: list[str], symbols=None, factor_names=None) -> dict[str, dict]:
        """批量加载多日因子值。返回 {date_str: {factor_name: Series}}."""
        cache: dict[str, dict] = {}
        trading_days = self._load_trading_days()
        sym_map = self._load_symbol_map()
        idx_to_sym = {v: k for k, v in sym_map.items()}

        years = set(int(d[:4]) for d in dates)
        date_to_idx = {d: trading_days.index(d) for d in dates if d in trading_days}

        # 预加载各因子各年的 parquet
        factor_year_dfs: dict[str, dict[int, pd.DataFrame]] = {}
        for fname in os.listdir(self._parquet_dir):
            if factor_names and fname not in factor_names:
                continue
            fdir = os.path.join(self._parquet_dir, fname)
            if not os.path.isdir(fdir):
                continue
            factor_year_dfs[fname] = {}
            for f in os.listdir(fdir):
                if f.endswith('.parquet'):
                    yr = int(f[:-len('.parquet')])
                    if yr in years:
                        factor_year_dfs[fname][yr] = pd.read_parquet(
                            os.path.join(fdir, f),
                            filters=[('date_i16', 'in', [date_to_idx[d] for d in dates if d[:4] == str(yr)])],
                        )

        for date_str in dates:
            if date_str not in date_to_idx:
                continue
            date_idx = date_to_idx[date_str]
            year = int(date_str[:4])
            fv = {}
            for fname, year_dfs in factor_year_dfs.items():
                df = year_dfs.get(year)
                if df is None or df.empty:
                    continue
                rows = df[df['date_i16'] == date_idx]
                if rows.empty:
                    continue
                series = pd.Series(
                    rows['value_f32'].values,
                    index=[idx_to_sym.get(int(s), str(s)) for s in rows['symbol_i16'].values],
                    name=fname,
                )
                if symbols:
                    series = series.reindex(symbols)
                fv[fname] = series
            if fv:
                cache[date_str] = fv
        return cache

    # ── 查询 ──

    def _get_existing_factors(self, date_str: str) -> set:
        """返回该日期已物化的因子名集合 (v471: 读因子 meta dates 集合, 免 parquet 逐日扫描)。"""
        existing = set()
        for fname in os.listdir(self._parquet_dir):
            fdir = os.path.join(self._parquet_dir, fname)
            if not os.path.isdir(fdir):
                continue
            meta = self._load_factor_meta(fname)
            if not meta or date_str not in set(meta.get("dates", [])):
                continue
            # source_hash 不匹配 → 因子代码已变, 该日期需重算 (per-factor 口径)
            if "source_hash" in meta and meta["source_hash"] != _source_hash_single(fname):
                continue
            existing.add(fname)
        return existing

    def is_materialized(self, date_range: list[str], factor_names: list[str]) -> bool:
        """检查是否所有日期都覆盖全部因子。"""
        if not date_range:
            return False
        check_dates = [date_range[0], date_range[-1]]
        step = max(1, len(date_range) // 20)
        for i in range(step, len(date_range) - 1, step):
            check_dates.append(date_range[i])
        return all(not self._date_missing_factors(d, factor_names) for d in check_dates)

    def _date_missing_factors(self, date_str: str, factor_names: list[str],
                              source_hash: str = None) -> list[str]:
        """返回该日期缺失的因子列表 (空 = 已完全物化)。"""
        existing = self._get_existing_factors(date_str)
        return sorted(set(factor_names) - existing)

    def list_cached_dates(self) -> list[str]:
        """返回缓存中全部已物化日期 (升序)。"""
        return self._load_trading_days()

    def latest_cached_date(self) -> str | None:
        """返回缓存中最新物化日期。"""
        dates = self.list_cached_dates()
        return dates[-1] if dates else None

    def trim_to_max_days(self, max_days: int) -> int:
        """删除超过 max_days 天前的旧缓存 (重映射 date_i16 保持全局索引一致)。"""
        if max_days <= 0:
            return 0
        latest = self.latest_cached_date()
        if not latest:
            return 0
        dates = self._load_trading_days()
        cutoff_idx = max(0, len(dates) - max_days)
        if cutoff_idx == 0:
            return 0
        cutoff_date = dates[cutoff_idx]
        _log.info("factor_cache: trim — cutoff=%s (max_days=%d, latest=%s)", cutoff_date, max_days, latest)
        deleted = 0
        # 删除超过 cutoff_idx 的日期在 parquet 中的行, 并重映射 date_i16 →
        # 新索引 (保全局交易日序号连续, 与 trading_days.json 一致)
        for fname in os.listdir(self._parquet_dir):
            fdir = os.path.join(self._parquet_dir, fname)
            if not os.path.isdir(fdir):
                continue
            for f in os.listdir(fdir):
                if not f.endswith('.parquet'):
                    continue
                ppath = os.path.join(fdir, f)
                df = pd.read_parquet(ppath)
                kept = df[df['date_i16'] >= cutoff_idx].copy()
                if len(kept) < len(df):
                    if len(kept) == 0:
                        os.remove(ppath)
                    else:
                        kept['date_i16'] = kept['date_i16'] - cutoff_idx
                        kept['date_i16'] = kept['date_i16'].astype('int16')
                        kept.to_parquet(ppath, compression='zstd', compression_level=3, index=False)
                    deleted += len(df) - len(kept)
        # 更新 trading_days (索引 0 = cutoff_date, 与重映射后的 date_i16 对齐)
        new_dates = dates[cutoff_idx:]
        self._save_trading_days(new_dates)
        # 因子 meta 的 dates 字段同样裁剪
        for fname in os.listdir(self._parquet_dir):
            meta = self._load_factor_meta(fname)
            if not meta or "dates" not in meta:
                continue
            meta["dates"] = [d for d in meta.get("dates", []) if d >= cutoff_date]
            if not meta["dates"]:
                meta.pop("dates", None)
            meta["first_date"] = meta["dates"][0] if meta.get("dates") else None
            meta["last_date"] = meta["dates"][-1] if meta.get("dates") else None
            self._save_factor_meta(fname, meta)
        return deleted

    # ── 维护 ──

    def _log_materialization(self, start, end, n_factors, n_symbols,
                             n_dates, n_rows, elapsed, force):
        try:
            record = {
                "ts": pd.Timestamp.now().isoformat(),
                "date_start": start, "date_end": end,
                "n_factors": n_factors, "n_symbols": n_symbols,
                "n_dates": n_dates, "n_rows": n_rows,
                "elapsed_sec": round(elapsed, 1), "force": bool(force),
            }
            with open(self._log_file, 'a') as f:
                f.write(json.dumps(record) + "\n")
        except Exception as _e:
            _log.warning("factor_cache: failed to log materialization: %s", _e)

    # ── checkpoint ──

    def _write_checkpoint(self, last_date: str, chunk_done: int, n_chunks: int,
                          failed_dates: list[str], source_hash: str):
        try:
            record = {
                "last_date": last_date,
                "chunk_done": chunk_done,
                "n_chunks": n_chunks,
                "failed_dates": list(failed_dates or []),
                "source_hash": source_hash,
                "ts": pd.Timestamp.now().isoformat(),
            }
            os.makedirs(os.path.dirname(self._checkpoint_path), exist_ok=True)
            with open(self._checkpoint_path, 'w') as f:
                json.dump(record, f)
        except Exception as e:
            _log.debug("checkpoint write failed (non-fatal): %s", e)

    def _read_checkpoint(self) -> dict | None:
        if not os.path.exists(self._checkpoint_path):
            return None
        try:
            with open(self._checkpoint_path, 'r') as f:
                data = json.load(f)
            ts = pd.Timestamp(data.get("ts", "1970-01-01"))
            ttl = _require_cfg("factor.compute.cache_checkpoint_ttl_sec")
            if (pd.Timestamp.now() - ts).total_seconds() > ttl:
                os.remove(self._checkpoint_path)
                return None
            return data
        except Exception:
            return None

    def _clear_checkpoint(self):
        try:
            if os.path.exists(self._checkpoint_path):
                os.remove(self._checkpoint_path)
        except Exception:
            pass

    # ── fundamentals panel ──

    def _build_fundamentals_panel(self, store, symbols: list[str],
                                  chunk_dates: list[str],
                                  data_full: pd.DataFrame = None) -> dict[str, pd.DataFrame]:
        """构建基本面 PIT panel，返回 {date_str: DataFrame(symbol×field)}。"""
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

        if not val_df.empty:
            val_df["date"] = pd.to_datetime(val_df["date"])
            val_piv = val_df.pivot(index="date", columns="symbol",
                                   values=["pe_ttm", "pb", "market_cap"]).ffill()
        else:
            val_piv = None

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

        _static_cols = {c: stocks_df[c] for c in stocks_df.columns
                        if c not in ("pe_ttm", "pb", "market_cap", "close_latest", "high_52w")}
        _static_index = stocks_df.index
        _fallback = {c: stocks_df[c] for c in ("pe_ttm", "pb", "market_cap")
                     if c in stocks_df.columns}

        result = {}
        for date_str in chunk_dates:
            ts = pd.Timestamp(date_str)
            _dyn = dict(_fallback)

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

            null_roe = df["roe"].isna() | (df["roe"] <= 0)
            if null_roe.any():
                pe_col = "pe_ttm" if "pe_ttm" in df.columns else "pe"
                if pe_col in df.columns:
                    derived = df["pb"] / df[pe_col].replace(0, None)
                    derived = derived.where((derived > 0) & (derived < 100))
                    df.loc[null_roe, "roe"] = derived.loc[null_roe]

            df.loc[df["pe"] <= 0, "pe"] = None
            df.loc[df["pe"] > 1000, "pe"] = None
            df.loc[df["pb"] <= 0, "pb"] = None

            result[date_str] = df

        return result
