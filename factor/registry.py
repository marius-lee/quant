"""因子注册表共享工具: 截面 z-score、DB 连接、基本面因子集合。"""

import numpy as np
import pandas as pd
import sqlite3
import os as _os
import atexit as _atexit
from typing import Optional
from config.constants import _require_cfg, _market_db_path


# ═══════════════════════════════════════════════════════════
# 共享连接
# ═══════════════════════════════════════════════════════════

_shared_limit_conn = None


def _close_shared():
    global _shared_limit_conn
    if _shared_limit_conn is not None:
        _shared_limit_conn.close()
        _shared_limit_conn = None


_atexit.register(_close_shared)


def _cs_zscore(series: pd.Series, min_count: int = None, sparse: bool = False) -> pd.Series:
    """截面 z-score 标准化: (x - cross_sectional_mean) / cross_sectional_std.
    若截面有效值 < min_count, 返回全 NaN。
    sparse=True 时使用 zscore_min_count_sparse (基本面因子), 否则使用 zscore_min_count_dense (价量因子)。"""
    if min_count is None:
        key = "factor.compute.zscore_min_count_sparse" if sparse else "factor.compute.zscore_min_count_dense"
        min_count = _require_cfg(key)
    if series.count() < min_count:
        return pd.Series(np.nan, index=series.index)
    std = series.std(ddof=1)
    if std == 0 or np.isnan(std):
        return pd.Series(np.nan, index=series.index)
    return (series - series.mean()) / std


def _db_connect():
    """模块级共享连接 + WAL 模式。"""
    conn = sqlite3.connect(_market_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
    return conn


# 需要 financials 三表的面板因子 (CSMAR: BS+IS+CF). P69: 集中化, 与 _FUNDAMENTAL_FN_MAP 同步维护
_FIN_FACTORS = {
    "roe_reported", "ocfp", "roa", "debt_ratio", "accruals", "asset_growth", "gp_ta",
    "sue", "holder_reduction", "pledge_ratio", "dividend_yield",
}
