"""因子缓存元数据：符号字典、交易日索引、分区元信息。"""
import json
import os
from pathlib import Path
import pandas as pd
from quant.data.store import DataStore
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

_log = get_logger("factor.store_metadata")

_CACHE_DIR = Path(_require_cfg("paths.factor_cache_dir", default="quant/data/factor_cache"))
_SYMBOL_DICT_PATH = _CACHE_DIR / "symbol_dict.json"
_TRADING_DAYS_PATH = _CACHE_DIR / "trading_days.json"


def ensure_symbol_dict(store: DataStore = None) -> dict[str, int]:
    """生成/加载全市场符号字典 {symbol: int16_id}。"""
    if _SYMBOL_DICT_PATH.exists():
        with open(_SYMBOL_DICT_PATH, 'r') as f:
            return json.load(f)
    _log.info("generating symbol_dict...")
    if store is None:
        store = DataStore()
    conn = store._connect()
    try:
        syms = [r[0] for r in conn.execute(
            "SELECT symbol FROM stocks ORDER BY symbol"
        ).fetchall()]
    finally:
        conn.close()
        store.close()
    sym_dict = {sym: i for i, sym in enumerate(syms)}
    with open(_SYMBOL_DICT_PATH, 'w') as f:
        json.dump(sym_dict, f)
    _log.info(f"symbol_dict generated: {len(sym_dict)} symbols")
    return sym_dict


def ensure_trading_days(store: DataStore = None) -> list[str]:
    """生成/加载全交易日列表（按日期升序），返回 list[str]，索引即全局序号。"""
    if _TRADING_DAYS_PATH.exists():
        with open(_TRADING_DAYS_PATH, 'r') as f:
            return json.load(f)
    _log.info("generating trading_days index...")
    if store is None:
        store = DataStore()
    conn = store._connect()
    try:
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily ORDER BY date"
        ).fetchall()]
    finally:
        conn.close()
        store.close()
    with open(_TRADING_DAYS_PATH, 'w') as f:
        json.dump(days, f)
    _log.info(f"trading_days generated: {len(days)} days")
    return days


def get_symbol_id(symbol: str) -> int:
    return ensure_symbol_dict()[symbol]


def get_date_index(date_str: str) -> int:
    return ensure_trading_days().index(date_str)


def partition_year(date_str: str) -> int:
    return int(date_str[:4])


def partition_path(factor: str, year: int) -> Path:
    """因子×年分区文件路径：parquet_f/{factor}/{year}.parquet"""
    base = _CACHE_DIR / "parquet_f" / factor
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{year}.parquet"


def meta_path(factor: str, year: int) -> Path:
    base = _CACHE_DIR / "parquet_f" / factor
    return base / f"{year}.meta.json"
