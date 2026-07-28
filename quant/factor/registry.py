"""因子注册表共享工具: 截面 z-score、DB 连接、基本面因子集合。"""

import numpy as np
import pandas as pd
import sqlite3
import os as _os
import atexit as _atexit
from typing import Optional
from quant.config.constants import _require_cfg, _market_db_path


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
    """截面稳健 z-score 标准化 (ADR-035 audit 2026-07-28 修复)。

    分层处理:
      1. 剔除 ±inf (上游除零/零填充段产 inf)
      2. Winsorize 1%/99% 分位 → 裁剪极端值 (单日涨跌停不再污染全截面)
      3. MAD (中位数绝对偏差) 标准化 → 比均值/std 更抗异常值

    来源: Barra USE4 风险模型 → MAD 标准化;
          Qlib / WorldQuant → winsorize 后再标准化。
    config factor.compute.winsorize_pct 控制裁剪分位 (默认 0.01 = 1%)。
    MAD 常数 1.4826 = 正态分布下 MAD→σ 的转换因子。

    sparse=True 时使用 zscore_min_count_sparse (基本面因子), 否则使用 zscore_min_count_dense (价量因子)。"""
    if min_count is None:
        key = "factor.compute.zscore_min_count_sparse" if sparse else "factor.compute.zscore_min_count_dense"
        min_count = _require_cfg(key)
    orig_index = series.index
    # 强制转换为 float (基本面因子列可能返回 object dtype → isfinite 报错)
    series = pd.to_numeric(series, errors='coerce')
    series = series[np.isfinite(series)]
    if series.count() < min_count:
        return pd.Series(np.nan, index=orig_index)

    # Winsorize: 裁剪极端值 (涨跌停/异常波动不再污染截面)
    pct = _require_cfg("factor.compute.winsorize_pct")
    if pct > 0 and series.count() > max(10, 1.0 / pct):
        lo = series.quantile(pct)
        hi = series.quantile(1.0 - pct)
        if hi > lo:
            series = series.clip(lo, hi)

    # MAD 标准化 (中位数绝对偏差) — 比 mean/std 抗异常值
    # 来源: Barra USE4 risk model; Rousseeuw & Croux (1993)
    median = series.median()
    mad = (series - median).abs().median()
    if mad == 0 or np.isnan(mad):
        # MAD=0 常见于稀疏因子 (如 zt_streak/dt_streak 大部分为 0)
        # 回退到 winsorized mean/std 标准化
        std = series.std(ddof=1)
        if std == 0 or np.isnan(std):
            return pd.Series(np.nan, index=orig_index)
        result = (series - series.mean()) / std
        return result.reindex(orig_index)
    result = (series - median) / (mad * 1.4826)
    return result.reindex(orig_index)


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
