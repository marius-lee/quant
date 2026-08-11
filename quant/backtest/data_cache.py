"""
Backtest Data Cache — 持久化回测预加载数据到磁盘，避免重复 DB 查询。

设计:
  - 缓存 key: (start_date, end_date, symbols_hash, lookback_days)
  - 存储格式: Parquet 分区 (按日期分区，列式压缩)
  - TTL: 配置可控，默认 7 天
  - 自动失效: 当 market.db 修改时间 > 缓存时间时自动失效
"""

import os
import hashlib
import pickle
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

import pandas as pd
import numpy as np

from quant.utils.logger import get_logger
from quant.config.paths import DATA_DIR
from quant.config.constants import _require_cfg

# Suppress ConstantInputWarning from scipy/pandas spearmanr on near-constant arrays
warnings.filterwarnings("ignore", message="An input array is constant")

_log = get_logger("backtest.data_cache")

# 缓存目录
_CACHE_DIR = Path(DATA_DIR) / "backtest_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# TTL 配置 (天)
_CACHE_TTL_DAYS = _require_cfg("backtest.cache_ttl_days", default=7)


def _compute_cache_key(
    start_date: str,
    end_date: str,
    symbols: List[str],
    lookback_days: int,
    universe_size: int = None,
) -> str:
    """生成缓存键。"""
    key_parts = [
        start_date,
        end_date,
        str(len(symbols)),
        hashlib.md5("".join(sorted(symbols)).encode()).hexdigest()[:16],
        str(lookback_days),
        str(universe_size or 0),
    ]
    return hashlib.md5("|".join(key_parts).encode()).hexdigest()[:32]


def _get_cache_paths(cache_key: str) -> Dict[str, Path]:
    """获取缓存文件路径。"""
    base = _CACHE_DIR / cache_key
    base.mkdir(parents=True, exist_ok=True)
    return {
        "data_full": base / "data_full.parquet",
        "benchmark": base / "benchmark.parquet",
        "fundamentals": base / "fundamentals.parquet",
        "meta": base / "meta.pkl",
    }


def _is_cache_valid(cache_key: str) -> bool:
    """检查缓存是否有效 (TTL + market.db 修改时间)。"""
    paths = _get_cache_paths(cache_key)
    meta_path = paths["meta"]
    
    if not meta_path.exists():
        return False
    
    try:
        with open(meta_path, "rb") as f:
            meta = pickle.load(f)
    except Exception:
        return False
    
    # 检查 TTL
    created_at = meta.get("created_at")
    if created_at:
        age = datetime.now() - created_at
        if age > timedelta(days=_CACHE_TTL_DAYS):
            return False
    
    # 检查 market.db 修改时间
    from quant.config.paths import MARKET_DB
    market_mtime = datetime.fromtimestamp(os.path.getmtime(MARKET_DB))
    if market_mtime > created_at:
        return False
    
    # 检查所有数据文件存在
    for key in ["data_full", "benchmark", "fundamentals"]:
        if not paths[key].exists():
            return False
    
    return True


def save_backtest_cache(
    cache_key: str,
    data_full: "pd.DataFrame",
    benchmark: "pd.Series",
    fundamentals: Dict[str, "pd.DataFrame"],
    metadata: Dict[str, Any],
) -> bool:
    """保存回测数据到缓存。"""
    try:
        paths = _get_cache_paths(cache_key)
        
        # 保存 data_full (分区写入)
        data_full.to_parquet(paths["data_full"], compression="zstd", compression_level=3)
        
        # 保存 benchmark
        benchmark.to_frame("close").to_parquet(paths["benchmark"], compression="zstd")
        
        # 保存 fundamentals (多个 DataFrame)
        fund_data = {}
        for k, v in fundamentals.items():
            if v is not None and not v.empty:
                fund_data[k] = v
        if fund_data:
            pd.to_pickle(fund_data, paths["fundamentals"])
        
        # 保存元数据
        meta = {
            "created_at": datetime.now(),
            "version": "1.0",
            "columns": list(metadata.get("columns", [])),
            "symbols_count": metadata.get("symbols_count", 0),
            "date_range": metadata.get("date_range", ""),
        }
        with open(paths["meta"], "wb") as f:
            pickle.dump(meta, f)
        
        return True
    except Exception as e:
        from quant.utils.logger import get_logger
        get_logger("backtest.data_cache").warning(f"Cache save failed: {e}")
        return False


def load_backtest_cache(cache_key: str) -> Optional[Dict[str, Any]]:
    """加载回测缓存。返回包含 data_full, benchmark, fundamentals 的字典。"""
    if not _is_cache_valid(cache_key):
        return None
    
    try:
        paths = _get_cache_paths(cache_key)
        
        data_full = pd.read_parquet(paths["data_full"])
        benchmark = pd.read_parquet(paths["benchmark"])["close"]
        
        fundamentals = {}
        if paths["fundamentals"].exists():
            fundamentals = pd.read_pickle(paths["fundamentals"])
        
        with open(paths["meta"], "rb") as f:
            meta = pickle.load(f)
        
        return {
            "data_full": data_full,
            "benchmark": benchmark,
            "fundamentals": fundamentals,
            "meta": meta,
        }
    except Exception as e:
        from quant.utils.logger import get_logger
        get_logger("backtest.data_cache").warning(f"Cache load failed: {e}")
        return None


def get_or_load_backtest_data(
    start_date: str,
    end_date: str,
    symbols: list,
    lookback_days: int,
    loader: callable,
    universe_size: int = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """获取或加载回测数据 (缓存优先，缺失则调用 loader 并缓存)。"""
    cache_key = _compute_cache_key(
        start_date, end_date, symbols, lookback_days, universe_size
    )
    
    if not force_refresh:
        cached = load_backtest_data(cache_key)
        if cached:
            from quant.utils.logger import get_logger
            get_logger("backtest.data_cache").info(f"Cache hit: {cache_key[:8]}")
            return cached
    
    # 缓存未命中，调用 loader
    data = loader()
    
    # 保存到缓存
    _save_to_cache(cache_key, data)
    
    return data


# 导入 os (用于 mtime 检查)
import os
