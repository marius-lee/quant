"""因子注册表共享工具: 截面 z-score、DB 连接、基本面因子集合。"""

import numpy as np
import pandas as pd
import sqlite3
import os as _os
from typing import Optional
from config.constants import _require_cfg, _market_db_path


# ═══════════════════════════════════════════════════════════
# 共享连接
# ═══════════════════════════════════════════════════════════

_shared_limit_conn = None


def _cs_zscore(series: pd.Series, min_count: int = None) -> pd.Series:
    """截面 z-score 标准化: (x - cross_sectional_mean) / cross_sectional_std.
    若截面有效值 < min_count, 返回全 NaN。"""
    if min_count is None:
        min_count = _require_cfg("factor.compute.zscore_min_count")
    if series.count() < min_count:
        return pd.Series(np.nan, index=series.index)
    return (series - series.mean()) / series.std(ddof=1)


def _db_connect():
    """模块级共享连接 + WAL 模式。"""
    conn = sqlite3.connect(_market_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# 需要 financials 三表的面板因子 (CSMAR: BS+IS+CF). P69: 集中化, 与 _FUNDAMENTAL_FN_MAP 同步维护
_FIN_FACTORS = {
    "roe_reported", "ocfp", "roa", "debt_ratio", "accruals", "asset_growth", "gp_ta",
    "sue", "holder_reduction", "pledge_ratio", "dividend_yield",
}
