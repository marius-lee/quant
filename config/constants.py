"""全局因子计算常量 — 统一从 config.yaml 读取，单一真相源。"""

import numpy as np
import pandas as pd
import sqlite3
import os as _os
from typing import Optional
from config.loader import get as _cfg


# ═══════════════════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════════════════

def _require_cfg(key):
    """Return config value for key. Raise if missing — no silent defaults."""
    val = _cfg(key)
    if val is None:
        raise KeyError(f"config.yaml missing required key: {key}")
    return val


_AMIHUD_WINDOW = _require_cfg("factor.windows.amihud")
_SKEWNESS_WINDOW = _require_cfg("factor.windows.skewness")
_VOLATILITY_WINDOW = _require_cfg("factor.windows.volatility")
_DOWNSIDE_VOL_WINDOW = _require_cfg("factor.windows.downside_volatility")
_IDIO_VOL_WINDOW = _require_cfg("factor.windows.idiosyncratic_vol")
_MAX_RET_WINDOW = _require_cfg("factor.windows.max_return")
_RANGE_WINDOW = _require_cfg("factor.windows.intraday_range")
_LHB_WINDOW = _require_cfg("factor.windows.lhb_net_buy")
_VOL_RATIO_SHORT = _require_cfg("factor.windows.volume_ratio_short")
_VOL_RATIO_LONG = _require_cfg("factor.windows.volume_ratio_long")

# ── 因子过滤/校验参数 (config: factor.*. 文献依据详见 config.yaml) ──
_AMIHUD_MIN_DAYS = _require_cfg("factor.amihud.min_valid_days")
_AMIHUD_MIN_RATIO = _require_cfg("factor.amihud.min_valid_ratio")
_AMIHUD_SCALE = _require_cfg("factor.amihud.scale")
_TURNOVER_FALLBACK = _require_cfg("factor.turnover_rev.fallback_count")
_IDIO_MIN_OBS = _require_cfg("factor.idio_vol.min_common_obs")
_H52W_CLIP_LOW = _require_cfg("factor.high52w.clip_low")
_H52W_CLIP_HIGH = _require_cfg("factor.high52w.clip_high")
_ROE_RATIO_MIN = _require_cfg("factor.roe_ratio.min")
_ROE_RATIO_MAX = _require_cfg("factor.roe_ratio.max")
_ROE_REP_MIN = _require_cfg("factor.roe_reported.min")
_ROE_REP_MAX = _require_cfg("factor.roe_reported.max")
_DEBT_MIN = _require_cfg("factor.debt_ratio.min")
_DEBT_MAX = _require_cfg("factor.debt_ratio.max")
_ACCRUALS_MIN = _require_cfg("factor.accruals.min")
_ACCRUALS_MAX = _require_cfg("factor.accruals.max")

def _market_db_path():
    """Return path to market.db — project-relative."""
    return _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "data", "market.db")

__all__ = [
    "_AMIHUD_WINDOW", "_SKEWNESS_WINDOW", "_VOLATILITY_WINDOW",
    "_DOWNSIDE_VOL_WINDOW", "_IDIO_VOL_WINDOW", "_MAX_RET_WINDOW",
    "_RANGE_WINDOW", "_LHB_WINDOW", "_VOL_RATIO_SHORT", "_VOL_RATIO_LONG",
    "_AMIHUD_MIN_DAYS", "_AMIHUD_MIN_RATIO", "_AMIHUD_SCALE",
    "_TURNOVER_FALLBACK", "_IDIO_MIN_OBS",
    "_H52W_CLIP_LOW", "_H52W_CLIP_HIGH",
    "_ROE_RATIO_MIN", "_ROE_RATIO_MAX", "_ROE_REP_MIN", "_ROE_REP_MAX",
    "_DEBT_MIN", "_DEBT_MAX", "_ACCRUALS_MIN", "_ACCRUALS_MAX",
    "_require_cfg", "_market_db_path",
]

# ═══════════════════════════════════════════════════════════
# Pipeline 状态映射 — 内部码 → 前端展示文本
# 单一真相源: 代码和前端共享此映射
# ═══════════════════════════════════════════════════════════

STATUS_LABELS = {
    # Step 1-5: generate_signals
    "data_updated":      "数据已更新",
    "data_loaded":       "行情已加载",
    "factors_computed":  "因子计算完成",
    "risk_filtered":     "风险过滤完成",
    "signals_generated": "盘前信号已生成",

    # Step 6: execute_signals
    "trades_executed":   "交易执行完成",

    # monitor (09:35-14:55)
    "monitor_active":    "盘中风控运行中",

    # attribution (15:30)
    "attribution_done":  "盘后归因完成",

    # idle states
    "idle_after_hours":  "盘后待命",
    "idle_pre_market":   "盘前等待",
    "idle_holiday":      "休市",
    "idle_weekend":      "周末",

    # error
    "error":             "系统异常",
}

def _status_label(code: str) -> str:
    """返回状态码对应的前端展示文本。未匹配返回原码。"""
    return STATUS_LABELS.get(code, code)


__all__ = [
    # ... existing exports
]
