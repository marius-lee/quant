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


def compute_market_beta_60d(data, date, window=None, benchmark_returns=None):
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


def compute_overnight_gap_5d(data, date, window=None):
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


def compute_vol_price_sync_20d(data, date, window=None):
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


def compute_revenue_growth_yoy(data, date, window=None, fundamentals=None, aux=None):
    """营收同比增长率 — 最近报告期 vs 去年同期 (真YoY, 非QoQ).
    来源: Foster (1977, JAR) — 季度盈利时间序列;
          Chan, Jegadeesh & Lakonishok (1996, JF) — 盈利动量.
    ADR-043 layer1: 优先从 aux["financial_income"] 取, 无 aux 时回退 DB.
    data: MultiIndex DataFrame (price path) 或简单 DataFrame (fundamentals path).
    """
    from collections import defaultdict
    if isinstance(data.columns, pd.MultiIndex):
        symbols = data["close"].columns.tolist() if "close" in data.columns.get_level_values(0) else []
    else:
        symbols = data.index.tolist()

    # 取所有报告期数据, 按 symbol 聚合
    sym_data = defaultdict(list)  # {symbol: [(stat_date, revenue), ...]}
    if aux is not None and "financial_income" in aux:
        fi = aux["financial_income"]
        if not fi.empty and "symbol" in fi.columns and "stat_date" in fi.columns:
            for _, r in fi.iterrows():
                sd = r["stat_date"]
                rev = r.get("operating_revenue")
                if sd and rev:
                    sym_data[r["symbol"]].append((pd.Timestamp(sd), rev))
    else:
        from quant.data.repos._base import DatabaseManager
        conn = DatabaseManager.market()
        rows = conn.execute(
            "SELECT symbol, stat_date, operating_revenue "
            "FROM financial_income WHERE stat_date <= ? "
            "ORDER BY symbol, stat_date DESC",
            (date,)
        ).fetchall()
        conn.close()
        for r in rows:
            if r[1] and r[2]:
                sym_data[r[0]].append((pd.Timestamp(r[1]), r[2]))

    result = {}
    yoy_tolerance = pd.Timedelta(days=45)  # 来源: 季报披露±45天窗口 (标准财务实践)
    for sym in symbols:
        rows = sorted(sym_data.get(sym, []), key=lambda x: x[0])
        if len(rows) < 2:
            continue
        latest_date, latest_rev = rows[-1]
        # 找去年同期: 同季度, 年份-1, ±45天
        target = latest_date - pd.DateOffset(years=1)
        prev_rev = None
        for sd, rev in reversed(rows[:-1]):  # 从近到远找
            if abs((sd - target).days) <= yoy_tolerance.days:
                prev_rev = rev
                break
        if prev_rev and prev_rev > 0 and latest_rev:
            result[sym] = latest_rev / prev_rev - 1

    if not result:
        return None
    s = pd.Series(result, dtype=float)
    s = s.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(s, sparse=True).rename("revenue_growth_yoy")


def compute_earnings_growth_yoy(data, date, window=None, fundamentals=None, aux=None):
    """净利润同比增长率 — 最近报告期 vs 去年同期 (真YoY, 非QoQ).
    来源: Foster (1977, JAR) — 季度盈利时间序列;
          Chan, Jegadeesh & Lakonishok (1996, JF) — 盈利动量.
    ADR-043 layer1: 优先从 aux["financial_income"] 取, 无 aux 时回退 DB.
    data: MultiIndex DataFrame (price path) 或简单 DataFrame (fundamentals path).
    """
    from collections import defaultdict
    if isinstance(data.columns, pd.MultiIndex):
        symbols = data["close"].columns.tolist() if "close" in data.columns.get_level_values(0) else []
    else:
        symbols = data.index.tolist()

    sym_data = defaultdict(list)
    if aux is not None and "financial_income" in aux:
        fi = aux["financial_income"]
        if not fi.empty and "symbol" in fi.columns and "stat_date" in fi.columns:
            for _, r in fi.iterrows():
                sd = r["stat_date"]
                np_val = r.get("net_profit")
                if sd and np_val is not None:
                    sym_data[r["symbol"]].append((pd.Timestamp(sd), np_val))
    else:
        from quant.data.repos._base import DatabaseManager
        conn = DatabaseManager.market()
        rows = conn.execute(
            "SELECT symbol, stat_date, net_profit "
            "FROM financial_income WHERE stat_date <= ? "
            "ORDER BY symbol, stat_date DESC",
            (date,)
        ).fetchall()
        conn.close()
        for r in rows:
            if r[1] and r[2] is not None:
                sym_data[r[0]].append((pd.Timestamp(r[1]), r[2]))

    result = {}
    yoy_tolerance = pd.Timedelta(days=45)  # 来源: 季报披露±45天窗口 (标准财务实践)
    for sym in symbols:
        rows = sorted(sym_data.get(sym, []), key=lambda x: x[0])
        if len(rows) < 2:
            continue
        latest_date, latest_np = rows[-1]
        target = latest_date - pd.DateOffset(years=1)
        prev_np = None
        for sd, np_val in reversed(rows[:-1]):
            if abs((sd - target).days) <= yoy_tolerance.days:
                prev_np = np_val
                break
        if prev_np is not None and abs(prev_np) > 1 and latest_np is not None:
            result[sym] = latest_np / prev_np - 1

    if not result:
        return None
    s = pd.Series(result, dtype=float).replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(s, sparse=True).rename("earnings_growth_yoy")


def compute_piotroski_fscore(data, date, window=None, fundamentals=None, aux=None):
    """Piotroski F-Score — 9项基本面质量综合打分 (0-9).
    window: 未使用 (price factor 调度兼容).

    A股 IC_IR≈0.3-0.5, 价值+质量复合, 低分=差, 高分=好.
    来源: Piotroski (2000) "Value Investing: The Use of Historical Financial
          Statement Information to Separate Winners from Losers", JAR.
          国泰君安 2021 A股验证.
    data: MultiIndex DataFrame (price path) 或简单 DataFrame (fundamentals path).
    """
    from collections import defaultdict, namedtuple

    # 命名元组消除魔术索引 (test-v338 原实现 bal[3] 把 total_liability 当 total_assets)
    FinRec = namedtuple('FinRec', 'symbol stat_date net_profit op_revenue op_cost '
                        'total_op_revenue total_profit op_profit')
    BalRec = namedtuple('BalRec', 'symbol stat_date total_assets total_liability '
                        'owner_equity fixed_assets intangible_assets')
    CfRec = namedtuple('CfRec', 'symbol stat_date net_op_cf')

    def _last_two(rows, cls):
        """取每 symbol 最近两期, 转为命名元组。rows 按 stat_date DESC 排序。"""
        d = defaultdict(list)
        for r in rows:
            if len(d[r[0]]) < 2:
                d[r[0]].append(cls(*r))
        return d

    if isinstance(data.columns, pd.MultiIndex):
        symbols = data["close"].columns.tolist() if "close" in data.columns.get_level_values(0) else []
    else:
        symbols = data.index.tolist()

    if aux is not None and all(t in aux for t in ["financial_income", "financial_balance", "financial_cashflow"]):
        fi = aux["financial_income"]
        fb = aux["financial_balance"]
        fc = aux["financial_cashflow"]
        fin_rows = [(r["symbol"], r["stat_date"], r.get("net_profit"), r.get("operating_revenue"),
                     r.get("operating_cost"), r.get("total_operating_revenue"), r.get("total_profit"),
                     r.get("operating_profit"))
                    for _, r in fi.iterrows()] if not fi.empty else []
        bal_rows = [(r["symbol"], r["stat_date"], r.get("total_assets"), r.get("total_liability"),
                     r.get("total_owner_equities"), r.get("fixed_assets"), r.get("intangible_assets"))
                    for _, r in fb.iterrows()] if not fb.empty else []
        cf_rows = [(r["symbol"], r["stat_date"], r.get("net_operate_cash_flow"))
                   for _, r in fc.iterrows()] if not fc.empty else []
    else:
        from quant.data.repos._base import DatabaseManager
        conn = DatabaseManager.market()
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
            "FROM financial_cashflow WHERE stat_date <= ? "
            "ORDER BY symbol, stat_date DESC",
            (date,)
        ).fetchall()
        conn.close()

    fin = _last_two(fin_rows, FinRec)
    bal = _last_two(bal_rows, BalRec)
    cf = _last_two(cf_rows, CfRec)

    # 组件参数来源: Piotroski (2000) JAR, Table 1 — 9项标准定义
    scores = {}
    for sym in symbols:
        if sym not in fin or len(fin[sym]) < 2:
            continue
        cur = fin[sym][0]    # 最新季度
        prv = fin[sym][1]    # 上期
        cb = bal[sym][0] if sym in bal and bal[sym] else None
        pb = bal[sym][1] if sym in bal and len(bal[sym]) > 1 else None
        cc = cf[sym][0] if sym in cf and cf[sym] else None

        score = 0
        # 1. ROA > 0: net_profit / total_assets > 0
        #    来源: Piotroski (2000), p.12 "ROA is defined as net income before
        #          extraordinary items scaled by beginning-of-year total assets"
        if cb and cb.total_assets and cb.total_assets > 0 and cur.net_profit:
            roa = cur.net_profit / cb.total_assets
            if roa > 0: score += 1

        # 2. CFO > 0: net_operate_cash_flow > 0
        #    来源: Piotroski (2000), p.12 "Cash flow from operations..."
        if cc and cc.net_op_cf and cc.net_op_cf > 0:
            score += 1

        # 3. ΔROA > 0: ROA 改善
        #    来源: Piotroski (2000), p.12 "Change in ROA..."
        if cb and pb and cb.total_assets and pb.total_assets and cur.net_profit and prv.net_profit:
            roa_cur = cur.net_profit / cb.total_assets
            roa_prev = prv.net_profit / pb.total_assets
            if roa_cur > roa_prev: score += 1

        # 4. CFO > ROA: 经营现金流质量 (应计利润低)
        #    来源: Piotroski (2000), p.12 "Accrual: CFO > ROA..."
        if cb and cb.total_assets and cc and cc.net_op_cf and cur.net_profit:
            if cc.net_op_cf / cb.total_assets > cur.net_profit / cb.total_assets:
                score += 1

        # 5. ΔLeverage ≤ 0: 长期负债率不上升
        #    来源: Piotroski (2000), p.12 "Change in leverage..."
        #    公式: total_liability / total_assets (资产负债率)
        if cb and pb and cb.total_assets and pb.total_assets:
            cur_lev = (cb.total_liability or 0) / cb.total_assets
            prev_lev = (pb.total_liability or 0) / pb.total_assets
            if cur_lev <= prev_lev: score += 1

        # 6. ΔLiquidity > 0: 流动比率改善
        #    来源: Piotroski (2000), p.12 "Change in liquidity..."
        #    数据限制: 无 current_assets/current_liability,
        #    改用 solvency ratio = total_assets / total_liability (资产偿债能力)
        #    高比率 = 资产覆盖负债能力强, 改善 = 正向信号
        if cb and pb and cb.total_liability and pb.total_liability:
            cur_solv = cb.total_assets / (cb.total_liability or 1)
            prev_solv = pb.total_assets / (pb.total_liability or 1)
            if cur_solv > prev_solv: score += 1

        # 7. ΔShares ≤ 0: 未增发新股
        #    来源: Piotroski (2000), p.12 "Change in shares outstanding..."
        #    数据限制: 无 total_shares, 用 equity_change - net_profit 判断:
        #    权益增加超过净利润 → 可能增发 → 负信号
        if cb and pb and cb.owner_equity and pb.owner_equity and cur.net_profit:
            equity_delta = cb.owner_equity - pb.owner_equity
            if equity_delta <= (cur.net_profit or 0): score += 1

        # 8. ΔGrossMargin > 0: 毛利率改善
        #    来源: Piotroski (2000), p.12 "Change in gross margin..."
        #    公式: (revenue - cost) / revenue
        if cur.op_revenue and cur.op_cost and prv.op_revenue and prv.op_cost:
            cur_gm = (cur.op_revenue - cur.op_cost) / (cur.op_revenue or 1)
            prev_gm = (prv.op_revenue - prv.op_cost) / (prv.op_revenue or 1)
            if cur_gm > prev_gm: score += 1

        # 9. ΔAssetTurnover > 0: 资产周转率改善
        #    来源: Piotroski (2000), p.12 "Change in asset turnover..."
        #    公式: operating_revenue / total_assets
        if cb and pb and cb.total_assets and pb.total_assets and cur.op_revenue:
            cur_at = cur.op_revenue / (cb.total_assets or 1)
            prev_at = prv.op_revenue / (pb.total_assets or 1) if prv.op_revenue else None
            if prev_at and cur_at > prev_at: score += 1

        scores[sym] = score

    if not scores:
        return None
    s = pd.Series(scores, dtype=float)
    return _cs_zscore(s, sparse=True).rename("piotroski_fscore")
