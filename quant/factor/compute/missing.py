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

    # 加载基准收益 (必须在 ret 检查之后、common_dates 使用之前)
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

    common_dates = ret.index.intersection(bm_ret.index)
    if len(common_dates) < window:
        if len(common_dates) < 10:
            return None
        window = len(common_dates)

    ret_aligned = ret.loc[common_dates[-window:]]
    bm_aligned = bm_ret.loc[common_dates[-window:]]

    beta = ret_aligned.cov(bm_aligned) / bm_aligned.var()
    return _cs_zscore(-beta.fillna(0), sparse=True).rename("market_beta_60d")


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


def compute_earnings_growth_yoy(data, date, fundamentals=None):
    """净利润同比增长率 — 最近报告期 vs 去年同期."""
    from quant.data.repos._base import DatabaseManager
    from collections import defaultdict
    conn = DatabaseManager.market()
    rows = conn.execute(
        "SELECT symbol, stat_date, net_profit "
        "FROM financial_income WHERE stat_date <= ? "
        "ORDER BY symbol, stat_date DESC",
        (date,)
    ).fetchall()
    conn.close()
    if not rows:
        return None
    latest, prev = {}, {}
    for r in rows:
        sym = r[0]
        if sym not in latest: latest[sym] = (r[1], r[2])
        elif sym not in prev and r[1] != latest[sym][0]: prev[sym] = (r[1], r[2])
    symbols = data["close"].columns if isinstance(data["close"], pd.DataFrame) else []
    result = {}
    for sym in symbols:
        if sym in latest and sym in prev:
            l_val, p_val = latest[sym][1], prev[sym][1]
            if l_val and p_val and abs(p_val) > 1:
                result[sym] = l_val / p_val - 1
    if not result:
        return None
    s = pd.Series(result, dtype=float).replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(s, sparse=True).rename("earnings_growth_yoy")


def compute_piotroski_fscore(data, date, fundamentals=None):
    """Piotroski F-Score — 9项基本面质量综合打分 (0-9).

    A股 IC_IR≈0.3-0.5, 价值+质量复合, 低分=差, 高分=好.
    来源: Piotroski (2000), 国泰君安 2021 A股验证.
    """
    from quant.data.repos._base import DatabaseManager
    conn = DatabaseManager.market()
    # 加载最近一年财务数据
    fin_rows = conn.execute(
        "SELECT symbol, stat_date, net_profit, operating_revenue, operating_cost, "
        "total_operating_revenue, total_profit, operating_profit "
        "FROM financial_income WHERE stat_date <= ? "
        "ORDER BY symbol, stat_date DESC",
        (date,)
    ).fetchall()
    bal_rows = conn.execute(
        "SELECT symbol, stat_date, total_assets, total_liability, total_owner_equities, "
        "fixed_assets, intangible_assets "
        "FROM financial_balance WHERE stat_date <= ? "
        "ORDER BY symbol, stat_date DESC",
        (date,)
    ).fetchall()
    cf_rows = conn.execute(
        "SELECT symbol, stat_date, net_operate_cash_flow "
        "FROM financial_cash_flow WHERE stat_date <= ? "
        "ORDER BY symbol, stat_date DESC",
        (date,)
    ).fetchall()
    conn.close()

    if not fin_rows:
        return None

    # 按股票分组,取最近两期
    from collections import defaultdict
    def _last_two(rows):
        d = defaultdict(list)
        for r in rows:
            if len(d[r[0]]) < 2:
                d[r[0]].append(r)
        return d

    fin = _last_two(fin_rows)
    bal = _last_two(bal_rows)
    cf = _last_two(cf_rows)

    symbols = data["close"].columns if isinstance(data["close"], pd.DataFrame) else []
    scores = {}
    for sym in symbols:
        if sym not in fin or len(fin[sym]) < 2:
            continue
        cur_fin, prev_fin = fin[sym][0], fin[sym][1]
        cur_bal = bal[sym][0] if sym in bal and bal[sym] else None
        prev_bal = bal[sym][1] if sym in bal and len(bal[sym]) > 1 else None
        cur_cf = cf[sym][0] if sym in cf and cf[sym] else None

        score = 0
        # 1. ROA > 0 (净利润/总资产)
        if cur_bal and cur_bal[3] and cur_bal[3] > 0 and cur_fin[2]:
            roa = cur_fin[2] / cur_bal[3]
            if roa > 0: score += 1
        # 2. CFO > 0 (经营现金流)
        if cur_cf and cur_cf[2] and cur_cf[2] > 0:
            score += 1
        # 3. ROA 改善
        if cur_bal and prev_bal and cur_bal[3] and prev_bal[3] and cur_fin[2] and prev_fin[2]:
            roa_cur = cur_fin[2] / cur_bal[3]
            roa_prev = prev_fin[2] / prev_bal[3]
            if roa_cur > roa_prev: score += 1
        # 4. CFO > ROA
        if cur_bal and cur_bal[3] and cur_cf and cur_cf[2] and cur_fin[2]:
            if cur_cf[2] / cur_bal[3] > cur_fin[2] / cur_bal[3]:
                score += 1
        # 5. 长期负债率不上升
        if cur_bal and prev_bal:
            cur_lev = (cur_bal[4] or 0) / (cur_bal[3] or 1)
            prev_lev = (prev_bal[4] or 0) / (prev_bal[3] or 1)
            if cur_lev <= prev_lev: score += 1
        # 6. 流动比率改善
        if cur_bal and prev_bal:
            cur_cr = ((cur_bal[3] or 0) - (cur_bal[5] or 0) - (cur_bal[6] or 0)) / ((cur_bal[4] or 1))
            prev_cr = ((prev_bal[3] or 0) - (prev_bal[5] or 0) - (prev_bal[6] or 0)) / ((prev_bal[4] or 1))
            if cur_cr > prev_cr: score += 1
        # 7. 不增发新股
        if cur_bal and prev_bal and cur_bal[5] and prev_bal[5]:
            if cur_bal[5] <= prev_bal[5]: score += 1
        # 8. 毛利率改善
        if cur_fin[3] and cur_fin[4] and prev_fin[3] and prev_fin[4]:
            cur_gm = (cur_fin[3] - cur_fin[4]) / (cur_fin[3] or 1)
            prev_gm = (prev_fin[3] - prev_fin[4]) / (prev_fin[3] or 1)
            if cur_gm > prev_gm: score += 1
        # 9. 资产周转率改善
        if cur_bal and prev_bal and cur_bal[3] and prev_bal[3] and cur_fin[3]:
            cur_at = cur_fin[3] / (cur_bal[3] or 1)
            prev_at = prev_fin[3] / (prev_bal[3] or 1)
            if cur_at > prev_at: score += 1

        scores[sym] = score

    if not scores:
        return None
    s = pd.Series(scores, dtype=float)
    return _cs_zscore(s, sparse=True).rename("piotroski_fscore")
