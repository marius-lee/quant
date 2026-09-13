"""Factor store helpers — constants and utility functions."""

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

# v626 fix: 4 levels up from quant/factor/store/helpers.py → project root
# (was 3, causing _CACHE_DIR = quant/quant/data/ — double quant)
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
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

# v546: 空结果聚合告警阈值 (交易日) — 单因子单轮空结果超阈值 → 判为异常 (代码/数据源),
# 而非正常缺数据; 避免 bug 因子静默 blocked 永久归档
_EMPTY_WARN_DAYS = 50


def _unblock_recovered(blocked: dict, per_factor: dict) -> None:
    """v546: 本轮成功算出结果的 (date, factor) 从 blocked 移除 — 恢复因子自动解除剔除.

    blocked 结构 {date_str: {factor: ts}}; 原地修改, 无返回.
    """
    for fname, dset in per_factor.items():
        for _d in dset:
            bfs = blocked.get(_d)
            if bfs and fname in bfs:
                del bfs[fname]
                if not bfs:
                    del blocked[_d]


def _empty_factor_summary(empty_factors, min_days: int = _EMPTY_WARN_DAYS):
    """v546: 本轮空结果按因子聚合, 返回 [(factor, 天数)] 按天数降序 (只含 >= min_days)."""
    from collections import Counter
    return sorted(((f, n) for f, n in Counter(f for _, f in empty_factors).items()
                   if n >= min_days), key=lambda x: -x[1])

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


def _materialize_mem_budget_gb() -> float:
    """物化内存预算(GB) = 物理内存总量 - 常驻预留 (config 驱动).

    8GB M1 实测常驻(桌面/Web服务port8521/其他App)占 ~4GB+, 须预留 headroom
    防止把系统推入 swap (watchdog kernel panic, 2026-08-30 实证).
    依赖 psutil; 缺失即 ImportError fail-fast (本项目 .venv 必装).

    Returns:
        float: 可供段进程并发使用的内存上限(GB), 下限 0.5 防除零.
    """
    import psutil
    _head = float(_require_cfg("factor.compute.materialize_mem_headroom_gb"))
    _total = psutil.virtual_memory().total / 1e9
    return max(0.5, _total - _head)


def _materialize_rss_gb(active: list) -> float:
    """当前物化父+子进程 RSS 总和(GB).

    Args:
        active: 活跃段进程列表, 元素为 (Popen, oj, oj_log, ws, we).

    Returns:
        float: 父进程 + 各子进程 RSS 总和(GB).
    """
    import psutil
    _self = psutil.Process().memory_info().rss / 1e9
    _child = 0.0
    for _proc, *_ in active:
        try:
            _child += psutil.Process(_proc.pid).memory_info().rss / 1e9
        except Exception:
            pass
    return _self + _child


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


