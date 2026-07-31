"""核心缺失因子补充 (test-v323).

实现 4 个高 IC_IR 但因缺日内/Level2 数据无法实现的替代因子:
  1. market_beta_60d      — 60日滚动 Beta vs 沪深300 (替代日内反转+机构拥挤)
  2. overnight_gap_5d     — 隔夜动量: 收盘→次日开盘, A股T+1结构专用
  3. vol_price_sync_20d   — 量价同步: 上涨日vs下跌日量价关系差异
  4. revenue_growth_yoy   — 营收同比增长率 (替代筹码分布的资金行为)
"""

import numpy as np
import pandas as pd
from quant.factor.registry import _cs_zscore
from quant.utils.logger import get_logger

_log = get_logger(__name__)


def compute_market_beta_60d(data, date, benchmark_returns=None):
    """市场 Beta — 60日滚动 OLS beta vs 沪深300."""
    if isinstance(data.columns, pd.MultiIndex):
        close = data["close"]
    else:
        close = data
    if close is None or close.empty:
        return None
    window = 60

    ret = close.pct_change().dropna(how="all")
    if len(ret) < window or len(common_dates) < window:
        # 数据不足 → 用最大可用窗口
        if len(common_dates) < 10:
            return None
        window = len(common_dates)

    # 加载基准收益
    if benchmark_returns is None:
        from quant.data.repos._base import DatabaseManager
        conn = DatabaseManager.market()
        rows = conn.execute(
            "SELECT date, close FROM benchmark_daily WHERE index_code='000300' ORDER BY date"
        ).fetchall()
        conn.close()
        bm = pd.Series({r[0]: r[1] for r in rows}, dtype=float).sort_index()
        bm_ret = bm.pct_change().dropna()
    else:
        bm_ret = benchmark_returns

    # 对齐日期计算滚动Beta
    common_dates = ret.index.intersection(bm_ret.index)
    if len(common_dates) < window:
        return None

    ret_aligned = ret.loc[common_dates[-window:]]
    bm_aligned = bm_ret.loc[common_dates[-window:]]

    # 滚动 beta = Cov(r_i, r_m) / Var(r_m)
    beta = ret_aligned.cov(bm_aligned) / bm_aligned.var()

    # 截面: 负beta溢价 (低beta股票未来收益更高)
    result = -beta.fillna(0)
    # 反转方向: 低beta → 高预期收益
    return _cs_zscore(result, sparse=True).rename("market_beta_60d")


def compute_overnight_gap_5d(data, date):
    """隔夜动量 — 最近5日收盘→次日开盘收益均值."""
    # MultiIndex columns: (field, symbol) — 直接索引
    if isinstance(data.columns, pd.MultiIndex):
        close = data["close"]
        opn = data["open"] if "open" in data.columns.get_level_values(0) else None
    else:
        close = data
        opn = None
    if close is None or opn is None or close.empty:
        return None

    # 隔夜收益 = 当日开盘 / 前日收盘 - 1
    overnight_ret = opn / close.shift(1) - 1
    # 5日平均
    gap_5d = overnight_ret.rolling(5, min_periods=3).mean().iloc[-1]
    gap_5d = gap_5d.replace([np.inf, -np.inf], np.nan)

    return _cs_zscore(gap_5d, sparse=True).rename("overnight_gap_5d")


def compute_vol_price_sync_20d(data, date):
    """量价同步 — 上涨日量价关系 vs 下跌日量价关系."""
    if isinstance(data.columns, pd.MultiIndex):
        close = data["close"]
        volume = data["volume"] if "volume" in data.columns.get_level_values(0) else None
    else:
        close = data
        volume = None
    if close is None or volume is None or close.empty:
        return None

    window = 20
    if len(close) < window:
        return None

    ret = close.pct_change().iloc[-window:]
    vol = volume.iloc[-window:]

    # 上涨日量价积 (正相关)
    up_mask = ret > 0
    up_sync = (ret * vol).where(up_mask).mean()

    # 下跌日量价积 (负相关)
    down_mask = ret < 0
    down_sync = (ret.abs() * vol).where(down_mask).mean()

    # 同步性 = 上涨日量价 / 下跌日量价 - 1
    # 负值: 放量下跌 > 放量上涨 → 看跌 (反转信号)
    sync = up_sync / down_sync.replace(0, np.nan) - 1
    sync = sync.replace([np.inf, -np.inf], np.nan)
    sync = sync.fillna(0)

    # 负向: 同步性低 → 未来反转概率高
    return _cs_zscore(-sync, sparse=True).rename("vol_price_sync_20d")


def compute_revenue_growth_yoy(data, date, fundamentals=None):
    """营收同比增长率 — 最近报告期 vs 去年同期."""
    import os
    from quant.data.repos._base import DatabaseManager
    conn = DatabaseManager.market()
    rows = conn.execute(
        "SELECT symbol, stat_date, operating_revenue "
        "FROM financial_income WHERE stat_date <= ? "
        "ORDER BY symbol, stat_date DESC",
        (date,)
    ).fetchall()
    conn.close()
    if not rows:
        return None

    from collections import defaultdict
    latest = {}
    prev = {}
    for r in rows:
        sym = r[0]
        if sym not in latest:
            latest[sym] = (r[1], r[2])
        elif sym not in prev and r[1] != latest[sym][0]:
            prev[sym] = (r[1], r[2])

    symbols = data["close"].columns if isinstance(data["close"], pd.DataFrame) else []
    result = {}
    for sym in symbols:
        if sym in latest and sym in prev:
            l_rev = latest[sym][1]
            p_rev = prev[sym][1]
            if l_rev and p_rev and p_rev > 0:
                result[sym] = l_rev / p_rev - 1

    if not result:
        return None
    s = pd.Series(result, dtype=float)
    s = s.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(s, sparse=True).rename("revenue_growth_yoy")
