"""FactorStore — 因子物化核心类."""
import gc
import json
import os
import time as _time
import multiprocessing as mp
import numpy as np
import pandas as pd
from quant.config.constants import _require_cfg
from quant.factor.compute._preload import preload_aux_data_chunk, slice_aux_for_date
from quant.factor.compute.price._alternative import clear_ztd_cache
from quant.factor.compute.price._preload import preload_ztd_cache
from quant.factor.store.helpers import (_log, _PROJ_ROOT, _CACHE_DIR, _PARQUET_DIR,
    _DATA_FULL, _PRIMS, _AUX_FULL, _FUNDAMENTALS, _SYMBOLS, _MISSING_MAP,
    _BLOCKED_PATH, _EMPTY_WARN_DAYS, _SOURCE_HASH_CACHE, _DATA_FINGERPRINT_CACHE,
    _unblock_recovered, _empty_factor_summary, _last_sqlite_date,
    _materialize_mem_budget_gb, _materialize_rss_gb, _compute_data_fingerprint,
    _source_hash_single, _compute_factor_source_hash)
from quant.factor.compute._dispatch import compute_all_factors
from quant.factor.windows import max_factor_calendar_days
from quant.utils.logger import get_logger
logger = get_logger("factor.store.core")
from quant.factor.store._query_mixin import FactorStoreQueryMixin
class FactorStore(FactorStoreQueryMixin):
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
        # v500 (perf): 因子 meta JSON 进程内缓存 — IC 覆盖检查逐日 os.listdir +
        # 逐因子读 metadata/*.json (重复磁盘 IO, ~95 因子 × 数百日期)。物化进程
        # 每次物化后 _save_factor_meta 会失效对应键, 本进程内跨日期复用。
        self._meta_cache: dict[str, dict] = {}
    # ── 元数据 ──
    def _metadata_path(self, name: str) -> str:
        return os.path.join(self._cache_dir, "metadata", f"{name}.json")
    def _load_json(self, path: str) -> dict:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return {}
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
        # v500 (perf): 进程内缓存, 避免 IC 覆盖检查/逐日缺失判定重复磁盘 JSON 读
        if factor_name in self._meta_cache:
            return self._meta_cache[factor_name]
        meta = self._load_json(self._metadata_path(f"factor_{factor_name}"))
        self._meta_cache[factor_name] = meta
        return meta
    def _save_factor_meta(self, factor_name: str, meta: dict):
        self._save_json(self._metadata_path(f"factor_{factor_name}"), meta)
        # 物化更新 meta 后失效缓存, 保证同进程后续读到新值
        self._meta_cache[factor_name] = meta
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
        """Worker 计算日期范围内的所有因子。
        返回 {factor: {date_i16: (symbol_i16 ndarray, value_f32 ndarray)}} —
        紧凑数组而非 Python tuple 列表 (v480: 消除逐行对象开销与 pickle 膨胀)。
        Args:
            start_idx/end_idx: 本 worker 负责的日期区间 [start, end)
            factor_names: 待算因子名列表
            date_list: 全量日期列表 (按全局序号索引)
            symbol_map: symbol → 全局 i16 序号 (跨运行持续累积)
            date_to_idx: date → 全局交易日序号
            source_hash: 因子源码 hash (透传回写)
        Returns:
            dict: {results, failed_dates, empty_factors, source_hash}
        """
        t0 = _time.time()
        global _DATA_FULL, _PRIMS, _AUX_FULL, _FUNDAMENTALS, _SYMBOLS
        if _DATA_FULL is None:
            raise RuntimeError("Worker: _DATA_FULL is None — fork failed")
        results: dict[str, dict[int, tuple[np.ndarray, np.ndarray]]] = {}
        for fname in factor_names:
            results[fname] = {}
        failed_dates: list[str] = []
        # v483: 空结果因子记录 (date, factor) — 父进程剔除 + 写 blocked, 不再静默 continue
        empty_factors: list[tuple[str, str]] = []
        for i in range(start_idx, end_idx):
            date_str = date_list[i]
            # v522: 进度埋点 — 每 10 天打点, 防止长 slice 完全静默 (5.5h 盲等事故)
            if (i - start_idx) % 10 == 0:
                _log.info("Worker [%s..%s]: %d/%d dates done (%.1fs elapsed)",
                          date_list[start_idx], date_str, i - start_idx, end_idx - start_idx,
                          _time.time() - t0)
            try:
                date_idx = date_to_idx[date_str]
                d_idx = np.int16(date_idx)
                # v483: todo 粒度为 (date, factor) — 只算 _MISSING_MAP 中该日期缺失的因子,
                # 已物化因子 (不在此 map 中) 跳过不重算
                missing = _MISSING_MAP.get(date_str)
                if not missing:
                    continue
                if not set(missing).issubset(set(factor_names)):
                    raise ValueError(
                        f"date {date_str}: missing factors {set(missing) - set(factor_names)} "
                        f"not in requested factor set")
                aux_sliced = slice_aux_for_date(_AUX_FULL, date_str) if _AUX_FULL else {}
                fund = _FUNDAMENTALS.get(date_str) if _FUNDAMENTALS else None
                fv = compute_all_factors(
                    _DATA_FULL, date_str,
                    primitives=_PRIMS,
                    fundamentals=fund,
                    preloaded_aux_chunk=aux_sliced,
                    factor_names=missing,          # v483: 只算缺失因子
                    status_filter=None,
                    factor_fail_fast=False,
                    quiet=True,
                    financials_cache={},
                )
                for fname in missing:
                    series = fv.get(fname)
                    if not isinstance(series, pd.Series):
                        empty_factors.append((date_str, fname))
                        continue
                    s = series.dropna()
                    if s.empty:
                        empty_factors.append((date_str, fname))
                        continue
                    # 向量化符号映射 (Index.map(dict) C 层 get_indexer), 替代逐行 items()
                    sym_idx = np.asarray(s.index.map(symbol_map), dtype=np.float64)
                    valid = ~np.isnan(sym_idx)
                    if not valid.any():
                        empty_factors.append((date_str, fname))
                        continue
                    sym_arr = sym_idx[valid].astype(np.int16)
                    val_arr = s.to_numpy(dtype=np.float32)[valid]
                    results[fname][d_idx] = (sym_arr, val_arr)
            except Exception as e:
                import traceback
                _log.error("Worker date %s failed for factor batch: %s", date_str, traceback.format_exc())
                failed_dates.append(date_str)
        _log.info("Worker %d-%d: computed %d dates in %.1fs (failed=%d, empty_factor_records=%d)",
                  start_idx, end_idx, end_idx - start_idx, _time.time() - t0,
                  len(failed_dates), len(empty_factors))
        return {"results": results, "failed_dates": failed_dates,
                "empty_factors": empty_factors, "source_hash": source_hash}
    def materialize(self,
                    date_range: list[str],
                    factor_names: list[str],
                    symbols: list[str],
                    store=None,
                    force: bool = False,
                    chunk_days: int = 200,
                    workers: int = None,
                    max_slice_days: int = None,
                    in_process: bool = False) -> dict:
        """批量物化因子值: fork pool + parquet column 分区写入。
        Args:
            date_range: 交易日列表
            factor_names: 因子名列表
            symbols: 股票列表
            store: DataStore 实例
            force: True 时删除旧数据重新物化
            chunk_days: 每块最大交易日数
            workers: 并行 worker 数 (默认 3-4)
            in_process: True 时跳过 subprocess 段并行, 主进程同步直算
                (复用 _materialize_sync). 由 Ray 分布式引擎调用 — Ray 已持有
                跨分区并行, 若再嵌套内部 subprocess 会核心过订/无加速.
        Returns:
            dict: {n_dates, n_factors, n_symbols, n_rows, elapsed_sec}
        """
        from quant.data.store import DataStore
        from quant.execution.calendar import is_trading_day
        if workers is None:
            # v525: 并发段进程数 — 每段峰值 ~1.5GB, 8GB 机器 3 并发 ≈ 5GB 总量
            workers = min(3, max(2, mp.cpu_count() // 2))
        _store_owned = store is None
        if store is None:
            store = DataStore()
        t0 = _time.time()
        # 过滤非交易日 — 只物化交易日，节假日/周末直接跳过，避免空算+FAILED 污染日志
        if date_range:
            original_len = len(date_range)
            from datetime import datetime
            date_range = [d for d in date_range if is_trading_day(datetime.strptime(d, "%Y-%m-%d").date())]
            if len(date_range) != original_len:
                _log.info("factor_cache: filtered %d non-trading days from input date_range (%d → %d)",
                          original_len - len(date_range), original_len, len(date_range))
        # v529: DuckDB 新鲜度断言 — 物化读 DuckDB 优先, 若副本落后于 SQLite
        # 会静默读旧值 (2026-08-18 实证: 手动补数后晚间链未跑, DuckDB 停 08-14,
        # 物化 08-17 全段 failed: missing primitive). daily_data 链先同步后物化,
        # 正常链序必过; 手动物化/链被跳过时 fail-fast 提示, 防 7 小时白算.
        if date_range and date_range[-1] >= _last_sqlite_date():
            from quant.data.duckdb_store import get_duckdb_manager
            _dk = get_duckdb_manager().query_df(
                "SELECT MAX(date) AS m FROM daily")["m"][0]
            if _dk is not None and date_range[-1] > str(_dk)[:10]:
                raise RuntimeError(
                    f"factor_cache: DuckDB daily 落后 ({str(_dk)[:10]} < {date_range[-1]})"
                    " — 先跑 bash scripts/duckdb_sync_all.sh 再物化")
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
        # 0.4 确定待算 (日期 → 缺失因子) — v483: todo 粒度为 (date, factor),
        # 只重算缺失因子, 不整日期白算; 缺数据 (空结果) 因子记入 blocked 剔除,
        # 防止反复重算永不收敛
        source_hash = _compute_factor_source_hash(set(factor_names))
        blocked = self._load_blocked()
        todo_map: dict[str, list[str]] = {}   # date → 待算缺失因子
        for date_str in sorted(date_range):
            if force:
                missing = list(factor_names)
            else:
                missing = self._date_missing_factors(date_str, factor_names, source_hash)
            if not missing:
                continue
            # v483-2: 剔除 blocked 因子 (上一轮已确认缺数据, 反复重算无意义)
            # v529: force 模式豁免 blocked — force 即"无条件重建"语义, 数据补齐后
            # 全量物化即可恢复 blocked 因子 (此前 blocked 无条件剔除导致因子
            # 数据补齐后永不重算 — 2026-08-18 ocfp 2020-2022 空窗实证).
            bl = set(blocked.get(date_str, {})) if not force else set()
            missing = [f for f in missing if f not in bl]
            if missing:
                todo_map[date_str] = missing
        if not todo_map:
            _log.info("factor_cache: all dates already fully materialized, skip")
            return {"n_dates": len(date_range), "n_factors": len(factor_names),
                    "n_symbols": len(symbols), "n_rows": 0, "elapsed_sec": 0,
                    "skipped": True, "failed_dates": []}
        # 0.5 checkpoint resume
        _ckpt = self._read_checkpoint()
        if _ckpt and _ckpt.get("last_date") and not force and _ckpt.get("source_hash") == source_hash:
            _resume_after = _ckpt["last_date"]
            _resume_retry = set(_ckpt.get("failed_dates") or [])
            todo_map = {d: v for d, v in todo_map.items()
                        if d > _resume_after or d in _resume_retry}
            _log.info("factor_cache: checkpoint resume — %d pending dates after %s (%d failed retry)",
                      len(todo_map), _resume_after, len(_resume_retry))
            if not todo_map:
                self._clear_checkpoint()
                return {"n_dates": 0, "n_factors": len(factor_names),
                        "n_symbols": len(symbols), "n_rows": 0, "elapsed_sec": 0,
                        "skipped": True, "failed_dates": []}
        # 0.6 执行: 归一 subprocess 段并行; store 注入 (测试/mock) 或
        # Ray 分布式引擎 (in_process) 时降级主进程同步直算 — subprocess
        # 无法继承内存数据源, 且 Ray 已持有跨分区并行, 嵌套 subprocess 会过订.
        if in_process or not _store_owned:
            return self._materialize_sync(
                store=store, date_list=sorted(todo_map.keys()),
                factor_names=factor_names, symbols=symbols, todo_map=todo_map,
                source_hash=source_hash, blocked=blocked, chunk_days=chunk_days,
                date_to_idx=date_to_idx, sym_map=sym_map)
        # 0.7 准备共享数据 (按 chunk 分块装载 — 控制内存, M1 8GB 硬约束)
        eff_days = max(_require_cfg("data.lookback_days"), max_factor_calendar_days(factor_names))
        date_list = sorted(todo_map.keys())
        _log.info("factor_cache: %d pending dates → %d workers × %dd lookback",
                  len(date_list), workers, eff_days)
        total_rows = 0
        n_dates_computed = 0
        failed_dates: list[str] = []
        per_factor_dates: dict[str, set] = {}
        _empty_this_run: list[tuple[str, str]] = []  # v546: 本轮空结果 (date, factor)
        n_chunks = max(1, (len(date_list) + chunk_days - 1) // chunk_days)
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
            # 本块内按日期分片分配给段进程 (每个段进程自载数据)
            # v525: 弃 fork+Pool+共享 DataFrame (COW 写风暴 + duckdb 线程
            # AfterFork 死循环 → 8GB 机上 309 jetsam kill 反复), 改
            # 独立 subprocess 段并行 — 每段 25 天数据自载, 峰值 ~1.5GB/段,
            # 并发 workers(默认3) ≈ 5GB 总量 < 8GB 墙.
            dates_in_chunk = chunk_dates
            slice_cap = max_slice_days or len(dates_in_chunk)
            n_worker_slices = min(
                max(1, -(-len(dates_in_chunk) // slice_cap)), len(dates_in_chunk))
            slice_size = -(-len(dates_in_chunk) // n_worker_slices)  # ceil 除, 覆盖全部日期
            slices = []
            for wi in range(n_worker_slices):
                _ws = wi * slice_size
                _we = min((wi + 1) * slice_size, len(dates_in_chunk))
                if _ws < _we:
                    slices.append((s + _ws, s + _we, wi, n_worker_slices))
            import subprocess
            import sys as _sys
            import tempfile
            import json as _json
            import pickle as _pickle
            import os as _os
            seg_tmp = tempfile.mkdtemp(prefix="factor_seg_")
            pending = []
            for _ws, _we, _wi, _ns in slices:
                _log.info("factor_cache: segment %d/%d (dates %s → %s, %d dates)",
                          _wi + 1, _ns, date_list[_ws], date_list[_we - 1], _we - _ws)
                seg_meta = {
                    "start_idx": _ws, "end_idx": _we, "date_list": date_list,
                    "factor_names": factor_names, "symbols": symbols,
                    "eff_days": eff_days, "source_hash": source_hash,
                    "cache_dir": self._cache_dir,
                    "data_start": (pd.Timestamp(date_list[0])
                                   - pd.Timedelta(days=eff_days)).strftime("%Y-%m-%d"),
                    "missing": {d: todo_map[d] for d in date_list[_ws:_we] if d in todo_map},
                }
                sj = _os.path.join(seg_tmp, f"seg_{_ws}_{_we}.json")
                oj = _os.path.join(seg_tmp, f"seg_{_ws}_{_we}.pkl")
                oj_log = _os.path.join(seg_tmp, f"seg_{_ws}_{_we}.log")
                with open(sj, "w") as _f:
                    _json.dump(seg_meta, _f)
                pending.append((sj, oj, oj_log, _ws, _we))
            def _consume_result_file(oj: str, oj_log: str, ws: int, we: int) -> None:
                """读取段结果并落盘 (逻辑同 v480 边收边写)."""
                nonlocal total_rows, n_dates_computed, part_seq
                if not _os.path.exists(oj):
                    _log.error("factor_cache: segment %d-%d no result file — 查看 %s",
                               ws, we, oj_log)
                    failed_dates.extend(date_list[ws:we])
                    return
                try:
                    with open(oj, "rb") as _f:
                        chunk_result = _pickle.load(_f)
                except Exception as _e:
                    _log.error("factor_cache: segment %d-%d pickle fail: %s (日志 %s)",
                               ws, we, _e, oj_log)
                    failed_dates.extend(date_list[ws:we])
                    return
                _log.info("factor_cache: consumed segment result: %d dates (%.1fs)",
                          len({d for rr in chunk_result["results"].values() for d in rr}),
                          _time.time() - t0)
                inc_rows, n_covered, part_seq, per_factor = self._consume_worker_result(
                    chunk_result, idx_to_date, part_seq, source_hash, set(factor_names))
                total_rows += inc_rows
                n_dates_computed += n_covered
                failed_dates.extend(chunk_result.get("failed_dates", []))
                for fname, dset in per_factor.items():
                    per_factor_dates.setdefault(fname, set()).update(dset)
                # v546: 成功重算 → 解除 blocked (恢复因子自动剔除失效)
                _unblock_recovered(blocked, per_factor)
                for _d, _f in chunk_result.get("empty_factors", []):
                    _empty_this_run.append((_d, _f))
                    if _d not in blocked:
                        blocked[_d] = {}
                    if _f not in blocked[_d]:
                        blocked[_d][_f] = _time.time()
                        _log.warning(
                            "factor_cache: factor %s blocked at %s — 计算为空结果 "
                            "(依赖数据缺失/不足), 已剔除后续重算; 数据补齐后自动恢复",
                            _f, _d)
            # v571: 内存守卫 — 按物理内存动态下调段并发, 防 swap→watchdog panic
            _per_w = float(_require_cfg("factor.compute.materialize_mem_per_worker_gb"))
            _budget = _materialize_mem_budget_gb()
            _cap = max(1, min(workers, int(_budget // _per_w)))
            if _cap != workers:
                _log.warning(
                    "factor_cache: mem guard — workers %d→%d (budget %.1fGB / %.1fGB per "
                    "worker, total RAM %.1fGB reserved headroom %.1fGB)",
                    workers, _cap, _budget, _per_w,
                    _budget + float(_require_cfg("factor.compute.materialize_mem_headroom_gb")),
                    float(_require_cfg("factor.compute.materialize_mem_headroom_gb")))
            else:
                _log.info("factor_cache: mem guard — workers=%d within budget %.1fGB", workers, _budget)
            workers = _cap
            _env = dict(_os.environ)
            _env["PYTHONPATH"] = _os.getcwd()
            active = []  # (proc, oj, oj_log, ws, we)
            try:
                for sj, oj, oj_log, ws, we in pending:
                    # 内存守卫: 并发达上限 或 当前物化RSS超预算时, 先等子进程退出释放内存
                    while len(active) >= workers or _materialize_rss_gb(active) > _budget:
                        _mem = _materialize_rss_gb(active) if len(active) >= 1 else 0.0
                        if _mem > _budget:
                            _log.warning(
                                "factor_cache: mem guard — RSS %.1fGB > budget %.1fGB, "
                                "hold launch until a segment finishes", _mem, _budget)
                        _proc, _oj, _ojl, _ws, _we = active.pop(0)
                        _rc = _proc.wait()
                        if _rc != 0:
                            _log.error("factor_cache: segment %d-%d exited rc=%d (日志 %s)",
                                       _ws, _we, _rc, _ojl)
                        _consume_result_file(_oj, _ojl, _ws, _we)
                    with open(oj_log, "wb") as _lof:
                        _proc = subprocess.Popen(
                            [_sys.executable, "-m", "quant.factor.materialize_segment",
                             "--seg", sj, "--out", oj],
                            env=_env, stdout=_lof, stderr=subprocess.STDOUT)
                    active.append((_proc, oj, oj_log, ws, we))
                while active:
                    _proc, _oj, _ojl, _ws, _we = active.pop(0)
                    _rc = _proc.wait()
                    if _rc != 0:
                        _log.error("factor_cache: segment %d-%d exited rc=%d (日志 %s)",
                                   _ws, _we, _rc, _ojl)
                    _consume_result_file(_oj, _ojl, _ws, _we)
            finally:
                clear_ztd_cache()
            self._write_checkpoint(chunk_dates[-1], ci + 1, n_chunks, list(failed_dates), source_hash)
            if blocked:
                self._save_blocked(blocked)
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
        # v483: 只报本次 todo 内 (缺失) 因子零覆盖 — 已物化因子不在 todo 属正常,
        # 不计入 "ZERO dates" 误报
        _todo_factors = {f for fs in todo_map.values() for f in fs}
        _zero_coverage = [n for n in _todo_factors
                          if n not in per_factor_dates or not per_factor_dates[n]]
        if _zero_coverage:
            _log.error("factor_cache: %d factors produced ZERO dates this run: %s",
                       len(_zero_coverage), ",".join(_zero_coverage))
        self._log_materialization(date_list[0], date_list[-1], len(factor_names), len(symbols),
                                  n_dates_computed, total_rows, elapsed, force)
        if failed_dates:
            _log.error("factor_cache: %d dates FAILED: %s", len(failed_dates),
                       ",".join(sorted(failed_dates)[:50]))
        # v483: 汇报 blocked 摘要 (缺数据被剔除的因子, 便于数据补齐后关注)
        _blocked_total = sum(len(v) for v in blocked.values()) if blocked else 0
        if _blocked_total:
            _log.warning("factor_cache: %d (date,factor) blocked (缺数据剔除) — 数据补齐后自动恢复",
                         _blocked_total)
        # v546: 本轮空结果按因子聚合, 超阈值 → ERROR 告警 (代码/数据源异常信号,
        # 而非正常缺数据) — 修复 bug 因子持续静默 blocked 归档
        _heavy_empty = _empty_factor_summary(_empty_this_run)
        if _heavy_empty:
            _log.error("factor_cache: %d factors produced >=%d empty dates this run "
                       "(非正常缺数据 — 检查代码或数据源): %s",
                       len(_heavy_empty), _EMPTY_WARN_DAYS,
                       ", ".join(f"{f}={n}" for f, n in _heavy_empty))
        return {"n_dates": n_dates_computed, "n_factors": len(factor_names),
                "n_symbols": len(symbols), "n_rows": total_rows,
                "elapsed_sec": round(elapsed, 1), "failed_dates": failed_dates}
    def _materialize_sync(self, store, date_list: list, factor_names: list,
                          symbols: list, todo_map: dict, source_hash: str,
                          blocked: dict, chunk_days: int,
                          date_to_idx: dict, sym_map: dict) -> dict:
        """v525: store 注入时的降级路径 — 主进程同步直算 (无 subprocess).
        测试/mock 数据源无法跨 subprocess 继承, 语义与 v525 前 fork 版一致
        (每 chunk 装载数据 → _worker_main → consume 落盘), 仅无并行。
        """
        from quant.factor.compute._preload import preload_aux_data_chunk
        from quant.factor.compute._primitives import precompute_primitives
        from quant.factor.compute.price._alternative import clear_ztd_cache
        from quant.factor.compute.price._preload import preload_ztd_cache
        t0 = _time.time()
        eff_days = max(_require_cfg("data.lookback_days"), max_factor_calendar_days(factor_names))
        global _DATA_FULL, _PRIMS, _AUX_FULL, _FUNDAMENTALS, _SYMBOLS, _MISSING_MAP
        _log.info("factor_cache: sync (store 注入) — %d dates × %dd lookback",
                  len(date_list), eff_days)
        total_rows = 0
        n_dates_computed = 0
        failed_dates: list[str] = []
        per_factor_dates: dict[str, set] = {}
        _empty_this_run: list[tuple[str, str]] = []  # v546: 本轮空结果 (date, factor)
        n_chunks = max(1, (len(date_list) + chunk_days - 1) // chunk_days)
        idx_to_date = {i: d for d, i in date_to_idx.items()}
        part_seq = 0
        for ci in range(n_chunks):
            s = ci * chunk_days
            e = min((ci + 1) * chunk_days, len(date_list))
            if s >= len(date_list):
                continue
            chunk_dates = date_list[s:e]
            _log.info("factor_cache: chunk %d/%d — %s .. %s (%d dates)",
                      ci + 1, n_chunks, chunk_dates[0], chunk_dates[-1], len(chunk_dates))
            data_start = (pd.Timestamp(chunk_dates[0]) - pd.Timedelta(days=eff_days)).strftime("%Y-%m-%d")
            _DATA_FULL = store.get_daily(symbols, start=data_start, end=chunk_dates[-1])
            _PRIMS = precompute_primitives(_DATA_FULL, factor_names=factor_names,
                                           save_disk_cache=False)
            try:
                bm_ret = store.get_benchmark("000300", start=data_start)
                if bm_ret is not None and not bm_ret.empty:
                    _PRIMS["benchmark_ret"] = bm_ret
            except Exception as _e:
                _log.warning("factor_cache: benchmark_ret unavailable (%s)", _e)
            preload_ztd_cache(chunk_dates, symbols)
            _AUX_FULL = preload_aux_data_chunk(symbols, chunk_dates[0], chunk_dates[-1])
            _FUNDAMENTALS = self._build_fundamentals_panel(
                store, symbols, chunk_dates, data_full=_DATA_FULL)
            _SYMBOLS = symbols
            _MISSING_MAP = {d: todo_map.get(d, []) for d in chunk_dates}
            chunk_result = self._worker_main(
                s, e, factor_names, date_list, sym_map, date_to_idx, source_hash)
            inc_rows, n_covered, part_seq, per_factor = self._consume_worker_result(
                chunk_result, idx_to_date, part_seq, source_hash, set(factor_names))
            total_rows += inc_rows
            n_dates_computed += n_covered
            failed_dates.extend(chunk_result.get("failed_dates", []))
            for fname, dset in per_factor.items():
                per_factor_dates.setdefault(fname, set()).update(dset)
            for _d, _f in chunk_result.get("empty_factors", []):
                blocked.setdefault(_d, {})[_f] = _time.time()
            self._write_checkpoint(chunk_dates[-1], ci + 1, n_chunks, list(failed_dates), source_hash)
            if blocked:
                self._save_blocked(blocked)
            clear_ztd_cache()
            gc.collect()
        if not failed_dates:
            self._clear_checkpoint()
        _merged = self._merge_pending_parts()
        if _merged:
            _log.info("factor_cache: merged %d part groups to main parquet", _merged)
        clear_ztd_cache()
        gc.collect()
        _log.info("factor_cache: materialized %d dates × %d factors → %d rows in %.1fs (sync)",
                  n_dates_computed, len(factor_names), total_rows, _time.time() - t0)
        return {"n_dates": n_dates_computed, "n_factors": len(factor_names),
                "n_symbols": len(symbols), "n_rows": total_rows,
                "elapsed_sec": round(_time.time() - t0, 1), "failed_dates": failed_dates}
    def _part_path(self, factor_name: str, year: int, part_id: int) -> str:
        """part 文件路径: {factor}/{year}.part{part_id} (无 .parquet 后缀,
        避免 trim_to_max_days/bulk_load 的 endswith('.parquet') 扫描误判)。"""
        return os.path.join(self._parquet_dir, factor_name, f"{year}.part{part_id}")
    def _consume_worker_result(self, chunk_result: dict, idx_to_date: dict,
                               part_seq: int, source_hash: str,
                               factor_names: set[str]) -> tuple[int, int, int, dict[str, set]]:
        """消费单个 worker 结果: 按 (factor, year) 分组直写 part + 更新 meta。
        v480: 随收随写 — 不累积全 chunk 结果, 父进程峰值 = 单 worker 紧凑数组。
        Args:
            chunk_result: worker 返回值 {results: {factor: {date_i16: (sym, val)}}, ...}
            idx_to_date: 全局序号 → 日期字符串
            part_seq: 全局 part 序号 (跨 chunk/slice 递增, 防覆盖)
            source_hash: 因子源码 hash
            factor_names: 全量因子名集合 (meta 写入口径)
        Returns:
            tuple: (inc_rows, n_covered_dates, part_seq, per_factor_dates)
        """
        inc_rows = 0
        covered: set[int] = set()
        per_factor: dict[str, set[int]] = {}
        for fname, date_rows in chunk_result["results"].items():
            if not date_rows:
                continue
            year_parts: dict[int, list[tuple[np.ndarray, np.ndarray, np.ndarray]]] = {}
            covered_dates: list[str] = []
            for d_idx, (sym_arr, val_arr) in date_rows.items():
                yr = int(idx_to_date[d_idx][:4])
                d_arr = np.full(len(sym_arr), d_idx, dtype=np.int16)
                year_parts.setdefault(yr, []).append((d_arr, sym_arr, val_arr))
                covered_dates.append(idx_to_date[d_idx])
                inc_rows += len(sym_arr)
            for yr, parts in year_parts.items():
                self._write_factor_date_part(fname, yr, part_seq, parts)
                part_seq += 1
            self._update_factor_meta(fname, covered_dates, source_hash, factor_names)
            per_factor[fname] = set(date_rows.keys())
            covered.update(date_rows.keys())
        return inc_rows, len(covered), part_seq, per_factor
    def _write_factor_date_part(self, factor_name: str, year: int,
                                part_id: int,
                                parts: list[tuple[np.ndarray, np.ndarray, np.ndarray]]) -> None:
        """写本 chunk 的 part 文件 (纯新增, 不读旧文件)。
        parts: [(date_i16 数组, symbol_i16 数组, value_f32 数组), ...] —
        单 (factor, year) 各日期紧凑数组直接拼接落盘, 无逐行 Python 对象。
        每 (factor, year, chunk) 一个独立 part; 全程结束后 _merge_pending_parts
        合并到主文件 {year}.parquet — 消除旧实现的整年度 read-modify-write 放大。
        """
        if not parts:
            return
        new_df = pd.DataFrame({
            'date_i16': np.concatenate([p[0] for p in parts]),
            'symbol_i16': np.concatenate([p[1] for p in parts]),
            'value_f32': np.concatenate([p[2] for p in parts]),
        })
        new_df['date_i16'] = new_df['date_i16'].astype('int16')
        new_df['symbol_i16'] = new_df['symbol_i16'].astype('int16')
        new_df['value_f32'] = new_df['value_f32'].astype('float32')
        ppath = self._part_path(factor_name, year, part_id)
        os.makedirs(os.path.dirname(ppath), exist_ok=True)
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
        meta["data_hash"] = _compute_data_fingerprint()
        existing = set(meta.get("dates", []))
        existing.update(covered_dates)
        meta["dates"] = sorted(existing)
        meta["first_date"] = meta["dates"][0] if meta["dates"] else None
        meta["last_date"] = meta["dates"][-1] if meta["dates"] else None
        self._save_factor_meta(factor_name, meta)
