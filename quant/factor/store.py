"""因子缓存存储 v480 — parquet 列式分区 + fork 共享内存 + checkpoint 续传。

设计原则:
  - 存储布局: factor_cache/parquet_f/{factor}/{year}.parquet (因子×年分区)
  - 列: date_i16 (全局交易日序号), symbol_i16 (字典 idx), value_f32 (float32)
  - 压缩: zstd level 3 — 相比 v469 按日期分区, 减少文件数量 (1886 → ~300 文件)
  - 多进程: fork 模式, 一次性继承 data_full/prims/aux/fundamentals (COW),
    单 Worker 顺序处理日期范围
  - 结果装配 (v480): worker 返回紧凑 numpy 数组 (symbol_i16/value_f32),
    父进程边收边写 — 消除 Python tuple 累积 + 全量 pickle 双份驻留
    (2026-08-13 全量回填实测 40+GB 卡死 macOS → B 方案修复, 峰值 ≈10GB)
  - checkpoint: 记录 last_date + failed_dates, resume 时加回重试
  - manifest: 每日期×因子 source_hash, 细粒度失效
  - 零 fallback: 读失败即抛, 无旧格式回退

对标: Qlib 因子库(parquet) + DolphinDB factorDB
"""
import os
import json
import time
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

# v483: 每日期待算缺失因子表 (fork COW 继承; worker 只算缺失因子, 杜绝整日期白算)
_MISSING_MAP: dict[str, list[str]] = {}

_BLOCKED_PATH = os.path.join(_CACHE_DIR, "blocked.json")

# per-factor 源码 hash 缓存 (代码不变则 hash 不变, 进程内安全缓存)
_SOURCE_HASH_CACHE: dict[str, str] = {}

# 输入数据指纹缓存 (进程内一次) — v492: 检测 daily/财务表数据变化触发重算
_DATA_FINGERPRINT_CACHE: dict[str, str] = {}


def _last_sqlite_date() -> str:
    """SQLite daily 最新日期 (v529 新鲜度断言用)."""
    from quant.data.repos._base import DatabaseManager
    mc = DatabaseManager.market()
    try:
        row = mc.execute("SELECT MAX(date) FROM daily").fetchone()
        return row[0] if row and row[0] else "1970-01-01"
    finally:
        mc.close()


def _compute_data_fingerprint(db_path: str = None) -> str:
    """计算输入数据指纹: daily 行数/turnover>0/amount>0/MAX(date) +
    财务三表 行数/MAX(stat_date)/MAX(pub_date)。

    v492: source_hash 只覆盖因子代码; 回填 amount/turnover/财务后已物化日期
    永不重算 → 缓存永远读旧值。本指纹纳入缺失判定: 指纹变化 → 该日期全部
    因子视为缺失 → 自动重算。指纹查询失败时返回 "" (与任何实值不等 →
    触发全量重算, 失败安全)。进程内缓存, 物化每晚只算一次。
    """
    if db_path is None:
        from quant.config.paths import MARKET_DB
        db_path = MARKET_DB
    cached = _DATA_FINGERPRINT_CACHE.get(db_path)
    if cached is not None:
        return cached
    import sqlite3
    h = hashlib.sha256()
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            r = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN turnover > 0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN amount > 0 THEN 1 ELSE 0 END), "
                "COALESCE(MAX(date),'') FROM daily"
            ).fetchone()
            h.update(f"daily:{r[0]}:{r[1]}:{r[2]}:{r[3]}".encode())
            # v492b: daily_valuation 也是基本面因子输入 (pe_ttm/pb/market_cap,
            # fundamental.py EPD/EPDS), 回填/修正后已物化日期须重算
            try:
                r = conn.execute(
                    "SELECT COUNT(*), "
                    "SUM(CASE WHEN market_cap > 0 AND market_cap IS NOT NULL THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN pe_ttm IS NOT NULL THEN 1 ELSE 0 END), "
                    "COALESCE(MAX(date),'') FROM daily_valuation"
                ).fetchone()
                h.update(f"daily_valuation:{r[0]}:{r[1]}:{r[2]}:{r[3]}".encode())
            except sqlite3.OperationalError:
                h.update(b"daily_valuation:missing")
            for tbl in ("financial_income", "financial_balance", "financial_cashflow"):
                try:
                    r = conn.execute(
                        f"SELECT COUNT(*), COALESCE(MAX(stat_date),''), "
                        f"COALESCE(MAX(pub_date),'') FROM {tbl}"
                    ).fetchone()
                    h.update(f"{tbl}:{r[0]}:{r[1]}:{r[2]}".encode())
                except sqlite3.OperationalError:
                    h.update(f"{tbl}:missing".encode())
        finally:
            conn.close()
    except Exception as e:
        _log.warning("factor_cache: data fingerprint query failed (%s) — 触发全量重算", e)
        return ""
    _DATA_FINGERPRINT_CACHE[db_path] = h.hexdigest()[:16]
    return _DATA_FINGERPRINT_CACHE[db_path]


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
        import time as _time
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
                    max_slice_days: int = None) -> dict:
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
            # v525: 并发段进程数 — 每段峰值 ~1.5GB, 8GB 机器 3 并发 ≈ 5GB 总量
            workers = min(3, max(2, mp.cpu_count() // 2))

        _store_owned = store is None
        if store is None:
            store = DataStore()

        t0 = _time.time()

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

        # 0.6 执行: 归一 subprocess 段并行; store 注入 (测试/mock) 时降级
        # 主进程同步直算 — subprocess 无法继承内存数据源
        if not _store_owned:
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
                for _d, _f in chunk_result.get("empty_factors", []):
                    if _d not in blocked:
                        blocked[_d] = {}
                    if _f not in blocked[_d]:
                        blocked[_d][_f] = time.time()
                        _log.warning(
                            "factor_cache: factor %s blocked at %s — 计算为空结果 "
                            "(依赖数据缺失/不足), 已剔除后续重算; 数据补齐后自动恢复",
                            _f, _d)

            _env = dict(_os.environ)
            _env["PYTHONPATH"] = _os.getcwd()
            active = []  # (proc, oj, oj_log, ws, we)
            try:
                for sj, oj, oj_log, ws, we in pending:
                    while len(active) >= workers:
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
        import time as _time
        from quant.factor.compute._preload import preload_aux_data_chunk
        from quant.factor.compute._primitives import precompute_primitives
        from quant.factor.compute.price._alternative import clear_ztd_cache, preload_ztd_cache

        t0 = _time.time()
        eff_days = max(_require_cfg("data.lookback_days"), max_factor_calendar_days(factor_names))
        global _DATA_FULL, _PRIMS, _AUX_FULL, _FUNDAMENTALS, _SYMBOLS, _MISSING_MAP
        _log.info("factor_cache: sync (store 注入) — %d dates × %dd lookback",
                  len(date_list), eff_days)

        total_rows = 0
        n_dates_computed = 0
        failed_dates: list[str] = []
        per_factor_dates: dict[str, set] = {}

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
            # data_hash 不匹配 → 输入数据已变 (amount/turnover/财务回填),
            # 该日期需重算 (v492) — meta 无 data_hash (旧版) 时按缺失处理
            if meta.get("data_hash") != _compute_data_fingerprint():
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

    def _load_blocked(self) -> dict:
        """读 blocked 记录 {date: {factor: ts}} — 缺数据空结果因子 (v483).

        date1: 记录在因子缓存目录 blocked.json; TTL 过期自动解除 (数据补齐后
        下一轮重算, 能算出非空结果即恢复正常); 零 fallback: 损坏按空处理。
        """
        if not os.path.exists(_BLOCKED_PATH):
            return {}
        try:
            with open(_BLOCKED_PATH, "r") as f:
                raw = json.load(f)
        except Exception as e:
            _log.warning("factor_cache: blocked.json unreadable, treating as empty: %s", e)
            return {}
        ttl = _require_cfg("factor.compute.cache_checkpoint_ttl_sec")   # 86400s = 1天
        now = time.time()
        out = {}
        for d, facs in raw.items():
            if not isinstance(facs, dict):
                continue
            alive = {f: ts for f, ts in facs.items()
                     if isinstance(ts, (int, float)) and now - ts < ttl}
            if alive:
                out[d] = alive
            else:
                _log.info("factor_cache: blocked expired for %s (%d records) — 重试",
                          d, len(facs) if isinstance(facs, dict) else 0)
        return out

    def _save_blocked(self, blocked: dict) -> None:
        """持久化 blocked 记录 (chunk 结束后写, 崩溃可续)."""
        try:
            os.makedirs(os.path.dirname(_BLOCKED_PATH), exist_ok=True)
            with open(_BLOCKED_PATH, "w") as f:
                json.dump(blocked, f, indent=1)
        except Exception as e:
            _log.warning("factor_cache: blocked save failed (non-fatal): %s", e)

    def _clear_blocked_for(self, date_str: str, factor_or_none: str = None) -> None:
        """数据补齐后手动解除 blocked (因子能算出非空结果时将不再 blocked; 此方法供故障排查调用)."""
        blocked = self._load_blocked()
        if date_str not in blocked:
            return
        if factor_or_none is None:
            blocked.pop(date_str, None)
        else:
            blocked.get(date_str, {}).pop(factor_or_none, None)
            if not blocked.get(date_str):
                blocked.pop(date_str, None)
        self._save_blocked(blocked)

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
        except Exception as e:
            _log.warning("factor_cache: checkpoint 读取失败, 续传失效从头跑: %s", e)
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

        # v502 (PIT industry): industry_history → per-date 最大段 Series.
        # 静态 stocks_df["industry"] (tushare 申万当前快照) 为后视, 逐日替换.
        _ih_static = None
        try:
            _ih_rows = pd.read_sql_query(
                "SELECT symbol, effective_from, industry FROM industry_history "
                "WHERE effective_from <= ? ORDER BY effective_from",
                mconn, params=(val_end,))
            if not _ih_rows.empty:
                _ih_static = _ih_rows.set_index(["symbol", "effective_from"])["industry"]
        except Exception:
            _ih_static = None

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

            # v502: industry PIT — 取该日期 effective_from<=ts 的最大段, 覆盖静态列
            if _ih_static is not None:
                try:
                    _sel = _ih_static[_ih_static.index.get_level_values("effective_from") <= ts.strftime("%Y-%m-%d")]
                    _last = _sel.groupby(level=0).last()
                    df["industry"] = df.index.map(_last.get)
                except Exception:
                    pass

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
