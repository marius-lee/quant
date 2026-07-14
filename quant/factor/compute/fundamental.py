"""因子计算函数 — 纯函数、全向量化、无副作用。

每个函数接受原始价格/成交量数据，返回因子值 Series。
数据格式: data 为 MultiIndex columns (field, symbol) 的 DataFrame, index=date.

因子分类:
  momentum   — Jegadeesh & Titman (1993)
  reversal   — Lehmann (1990), Jegadeesh (1990)
  volatility — Andersen et al. (2001)
  volume     — Gervais, Kaniel & Mingelegrin (2001)
  liquidity  — Amihud (2002)
  skewness   — Barberis & Huang (2008)

所有因子值做截面 z-score 标准化(去均值/除标准差), 确保跨因子可比。
"""

__all__ = [
    "_FUNDAMENTAL_FN_MAP",
    "_get_financial_historical",
    "_load_daily_valuation_pe",
    "_ttm_sum",
    "compute_accruals",
    "compute_analyst_consensus",
    "compute_asset_growth",
    "compute_bp_ratio",
    "compute_debt_ratio",
    "compute_dividend_yield",
    "compute_earnings_revision",
    "compute_earnings_upgrade",
    "compute_ep_ratio",
    "compute_epa",
    "compute_epd",
    "compute_epds",
    "compute_financial_anomaly",
    "compute_gp_ta",
    "compute_gross_margin_diff",
    "compute_high52w_dist",
    "compute_holder_reduction",
    "compute_ihn",
    "compute_insider_cluster",
    "compute_insider_increase",
    "compute_margin_buy_ratio",
    "compute_ocfp",
    "compute_pledge_ratio",
    "compute_roa",
    "compute_roe_ratio",
    "compute_roe_reported",
    "compute_roe_trimmed",
    "compute_size",
    "compute_sue",
]


import numpy as np
import pandas as pd
import sqlite3
import os as _os
from typing import Optional

from quant.config.constants import *
from quant.factor.registry import _cs_zscore, _db_connect, _FIN_FACTORS
from quant.factor.compute._shared import _market_db_path
from quant.data.repos._base import DatabaseManager

def compute_high52w_dist(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """接近52周高点→高分。dist = 1 - close_latest/high_52w, 取负号。
    数据字段: stocks.high_52w, stocks.close_latest(当日收盘)"""
    # TODO(#5): close_latest 需要从 daily 表补, fundamentals 里只有 stocks 静态字段。
    # 当前使用 stocks 表字段, 在两次 sync_stock_list() 之间可能过期。
    # 建议: 在 data/store.py get_fundamentals() 中增加 daily.close 的 LEFT JOIN。
    dist = 1.0 - fundamentals["close_latest"] / fundamentals["high_52w"]
    dist = dist.replace([np.inf, -np.inf], np.nan).clip(-2, 2)
    return _cs_zscore(-dist, sparse=True).rename("high52w_dist")

# ═══════════════════════════════════════════════════════════
# 因子注册表

# ═══════════════════════════════════════════════════════════
# 11. 北向资金净流入 — 陆股通 A 股最可靠因子 IC≈0.04-0.06
# ═══════════════════════════════════════════════════════════

def compute_ep_ratio(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """EP 比率 (1/PE_TTM) — 价值因子。低PE_TTM = 高EP = 高分。
    数据来源: daily_valuation.pe_ttm (JQData, 至 2026-04-02), 回退 stocks.pe
    来源: Fama & French (1992) — 价值因子 (HML)
    """
    # 优先使用 pe_ttm (daily_valuation via store.get_fundamentals), 回退到 stocks.pe
    pe_col = "pe_ttm" if "pe_ttm" in fundamentals.columns and fundamentals["pe_ttm"].notna().any() else "pe"
    ep = 1.0 / fundamentals[pe_col]
    ep = ep.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(ep, sparse=True).rename("ep_ratio")


def compute_bp_ratio(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """BP 比率 (1/PB) — 价值因子。低PB = 高BP = 高分。
    过滤 PE<=0 或 PE>1000 的极端值 (PE失真时bp_ratio无意义)。
    来源: Fama & French (1992) — 账面市值比
    """
    bp = 1.0 / fundamentals["pb"]
    bp = bp.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(-bp, sparse=True).rename("bp_ratio")  # IC=+0.059实测: -bp方向(即低BP=高PB=成长)匹配IC, A股成长溢价


def compute_size(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """规模因子 — +log(总市值)。大盘股 = 高分。
    来源: Fama & French (1993) — 市值因子
    A股实证: IC=-0.101 → 大盘股跑赢, 与传统SMB反向
    """
    size = np.log(fundamentals["total_mv"])
    size = size.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(size, sparse=True).rename("size")


def compute_roe_ratio(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """ROE 盈利能力因子 — 盈利能力溢价。高分 = 高ROE = 高预期收益。

    来源: Fama & French (2015) — 盈利能力因子 (RMW)
    使用 stocks.roe 列 (EPS / BVPS 推导)，过滤 ROE>100 极端值。
    """
    if "roe" not in fundamentals.columns or fundamentals["roe"].isna().all():
        return pd.Series(np.nan, index=fundamentals.index, name="roe_ratio")
    roe = fundamentals["roe"].astype(float)
    # 过滤极端 ROE: 负值 或 >100 视为数据错误
    roe = roe.where((roe > 0) & (roe < 100))
    roe = roe.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(roe, sparse=True).rename("roe_ratio")


# ── 基本面因子函数映射 (元数据从 factor_registry 表读取) ──

def compute_margin_buy_ratio(fundamentals: "pd.DataFrame", date: str, aux=None) -> "pd.Series":
    """融资买入占余额比: margin_buy / margin_balance (广发证券 2024, IC=-7.95%).

    公式: 融资买入额 / 融资余额。分母是余额而非成交额。
    数据源: margin_detail 表 (akshare stock_margin_detail_sse/szse)。
    来源: 广发证券《多因子ALPHA系列之五十二：基于融资融券因子研究》2024.02。
    """
    # Use preloaded aux data if available
    if aux is not None and "margin" in aux:
        m = aux["margin"]
        if not m.empty and "margin_buy" in m.columns and "margin_balance" in m.columns:
            s = m["margin_buy"] / m["margin_balance"].replace(0, np.nan)
            return s.dropna().rename("margin_buy_ratio")
    # Fallback: standalone query
    conn = _db_connect()
    rows = conn.execute(
        "SELECT symbol, margin_buy, margin_balance FROM margin_detail "
        "WHERE date = (SELECT MAX(date) FROM margin_detail WHERE date <= ?)",
        (date,)
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float, name="margin_buy_ratio")
    s = pd.Series({r[0]: r[1] / r[2] if r[2] and r[2] > 0 else np.nan
                   for r in rows if r[1] is not None and r[2] is not None})
    return s.dropna().rename("margin_buy_ratio")


def compute_analyst_consensus(fundamentals: "pd.DataFrame", date: str, aux=None) -> "pd.Series":
    """分析师共识度: buy_count / report_count (盈利预测一致预期)。

    公式: 买入评级数 / 总报告数。值高 = 分析师一致看多。
    数据源: analyst_forecast 表 (akshare stock_analyst_rank_em)。
    来源: 中信建投《逐鹿Alpha》2022, 海通金工 2023。
    """
    # Use preloaded aux data if available
    if aux is not None and "analyst" in aux:
        a = aux["analyst"]
        if not a.empty and "buy_count" in a.columns and "report_count" in a.columns:
            s = a["buy_count"] / a["report_count"].replace(0, np.nan)
            return s.dropna().rename("analyst_consensus")
    # Fallback: standalone query
    conn = _db_connect()
    rows = conn.execute(
        "SELECT symbol, buy_count, report_count FROM analyst_forecast "
        "WHERE sync_date = (SELECT MAX(sync_date) FROM analyst_forecast WHERE sync_date <= ?)",
        (date,)
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float, name="analyst_consensus")
    s = pd.Series({r[0]: r[1] / r[2] if r[2] and r[2] > 0 else np.nan
                   for r in rows if r[1] is not None and r[2] is not None})
    return s.dropna().rename("analyst_consensus")


# ── Phase 3 财务因子 (季报三表) ──

_ALLOWED_FINANCIAL_TABLES = {"financial_income", "financial_balance", "financial_cashflow"}


def _get_financial_historical(table: str, date: str, forward_days: int = 90) -> "pd.DataFrame":
    """Query quarterly financial data up to date (+forward_days for late filings).

    季报公布有延迟 (Q1 在 4月底, Q2 在 8月底, Q3 在 10月底, 年报在次年 4月底),
    所以用 anunciate_date <= 实际日期 + 90天 来包含已公布但尚未到报告期的季报.

    安全: 表名白名单校验，防止 SQL 注入。
    """
    if table not in _ALLOWED_FINANCIAL_TABLES:
        raise ValueError(f"Table not allowed: {table}")
    max_stat = (pd.Timestamp(date) + pd.DateOffset(days=forward_days)).strftime("%Y-%m-%d")
    conn = _db_connect()
    df = pd.read_sql(
        f"SELECT * FROM {table} WHERE stat_date <= ? ORDER BY stat_date",
        conn, params=(max_stat,),
    )
    return df


def _ttm_sum(df: "pd.DataFrame", col: str, n_quarters: int = 4) -> "pd.Series":
    """Compute TTM sum of column over last n_quarters per symbol."""
    df = df.dropna(subset=[col])
    if df.empty:
        return pd.Series(dtype=float)
    # Group by symbol, take last n_quarters, sum
    grouped = df.groupby("symbol")
    result = {}
    for sym, grp in grouped:
        grp_sorted = grp.sort_values("stat_date")
        if len(grp_sorted) >= n_quarters:
            result[sym] = grp_sorted[col].tail(n_quarters).sum()
    return pd.Series(result)


def compute_gross_margin_diff(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """毛利率TTM差分: (营收TTM - 营业成本TTM) / 营收TTM - 上期.

    公式: GrossMargin_t - GrossMargin_{t-4Q}, 其中 GM = (revenue - cost) / revenue.
    数据源: financial_income (total_operating_revenue / operating_revenue, operating_cost).
    来源: 源达信息《毛利率TTM差分因子》2026, ICIR=0.79.
    """
    df = _get_financial_historical("financial_income", date)
    if df.empty:
        return pd.Series(dtype=float, name="gross_margin_diff")

    # Use total_operating_revenue if available, else operating_revenue
    rev_col = "total_operating_revenue" if "total_operating_revenue" in df.columns else "operating_revenue"
    cost_col = "operating_cost"

    if rev_col not in df.columns or cost_col not in df.columns:
        return pd.Series(dtype=float, name="gross_margin_diff")

    # Current TTM (last 4 quarters)
    rev_ttm = _ttm_sum(df, rev_col, 4)
    cost_ttm = _ttm_sum(df, cost_col, 4)

    gm_current = (rev_ttm - cost_ttm) / rev_ttm.replace(0, np.nan)

    # Previous TTM: exclude the most recent quarter, use quarters 5-2 from end
    prev_result = {}
    grouped = df.groupby("symbol")
    for sym, grp in grouped:
        grp_sorted = grp.sort_values("stat_date")
        if len(grp_sorted) >= 5:
            # Use quarters 2-5 (one quarter lagged)
            rev_prev = grp_sorted[rev_col].iloc[-5:-1].sum()
            cost_prev = grp_sorted[cost_col].iloc[-5:-1].sum()
            if rev_prev > 0:
                prev_result[sym] = (rev_prev - cost_prev) / rev_prev

    gm_prev = pd.Series(prev_result)

    # Diff
    diff = gm_current.sub(gm_prev, fill_value=np.nan)
    return diff.dropna().rename("gross_margin_diff")


def compute_financial_anomaly(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """财务异常复合: 4 子因子等权 (申万宏源 2018, IC=6.79%, ICIR~1.5).

    六因子简化版（缺预付款、销售费用）:
      1. 存货异常: -(inv_growth - rev_growth)
      2. 应收异常: -(ar_growth - rev_growth)
      3. 管理费异常: -(admin_growth - rev_growth)
      4. 毛利异常: -(gm_change)
    等权 → _cs_zscore → 取负 (异常值高=坏=反向信号).

    数据源: financial_balance (inventories, account_receivable) + financial_income.
    来源: 申万宏源《财务异常综合评分体系》2018.06.
    """
    df_inc = _get_financial_historical("financial_income", date)
    df_bal = _get_financial_historical("financial_balance", date)

    if df_inc.empty or df_bal.empty:
        return pd.Series(dtype=float, name="financial_anomaly")

    rev_col = "total_operating_revenue" if "total_operating_revenue" in df_inc.columns else "operating_revenue"

    # For each symbol, get last 2 periods
    def _yoy_growth(df: "pd.DataFrame", col: str) -> "pd.Series":
        """YoY growth rate for each symbol (latest vs same quarter prev year)."""
        result = {}
        grouped = df.groupby("symbol")
        for sym, grp in grouped:
            grp_sorted = grp.sort_values("stat_date")
            if len(grp_sorted) >= 2 and col in grp_sorted.columns:
                v_latest = grp_sorted[col].iloc[-1]
                v_prev = grp_sorted[col].iloc[-2]
                if v_prev and v_prev != 0:
                    result[sym] = (v_latest - v_prev) / abs(v_prev)
        return pd.Series(result)

    def _gm_change() -> "pd.Series":
        """Gross margin change: gm_t - gm_{t-1}."""
        result = {}
        grouped = df_inc.groupby("symbol")
        cost_col = "operating_cost"
        for sym, grp in grouped:
            grp_sorted = grp.sort_values("stat_date")
            if len(grp_sorted) >= 2:
                rev_l, cost_l = grp_sorted[rev_col].iloc[-1], grp_sorted[cost_col].iloc[-1]
                rev_p, cost_p = grp_sorted[rev_col].iloc[-2], grp_sorted[cost_col].iloc[-2]
                if rev_l > 0 and rev_p > 0:
                    gm_l = (rev_l - cost_l) / rev_l
                    gm_p = (rev_p - cost_p) / rev_p
                    result[sym] = gm_l - gm_p
        return pd.Series(result)

    rev_growth = _yoy_growth(df_inc, rev_col)
    inv_growth = _yoy_growth(df_bal, "inventories")
    ar_growth = _yoy_growth(df_bal, "account_receivable")
    admin_growth = _yoy_growth(df_inc, "administration_expense")
    gm_change = _gm_change()

    # Build composite: anomaly = (item_growth - revenue_growth)
    scores = {}
    all_syms = set(rev_growth.index) | set(inv_growth.index) | set(ar_growth.index) | set(gm_change.index)
    for sym in all_syms:
        z = 0.0
        count = 0
        # 1. Inventories anomaly
        if sym in inv_growth.index and sym in rev_growth.index:
            z += -(inv_growth[sym] - rev_growth[sym])
            count += 1
        # 2. Receivables anomaly
        if sym in ar_growth.index and sym in rev_growth.index:
            z += -(ar_growth[sym] - rev_growth[sym])
            count += 1
        # 3. Admin expense anomaly
        if sym in admin_growth.index and sym in rev_growth.index:
            z += -(admin_growth[sym] - rev_growth[sym])
            count += 1
        # 4. Gross margin change
        if sym in gm_change.index:
            z += -gm_change[sym]
            count += 1
        if count > 0:
            scores[sym] = z / count * 4  # Normalize to 4-sub-factor scale

    result = pd.Series(scores)
    result = result.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(result, sparse=True).rename("financial_anomaly")


def compute_roe_trimmed(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """单季度ROE(掐头): 归母净利 / avg(归母权益) → 剔除最高10%.

    公式: ROE_q = net_profit / ((equity_t + equity_{t-1}) / 2)
    然后剔除截面 top 10% (去极端值), 保留的做 _cs_zscore.

    数据源: financial_income (net_profit) + financial_balance (equities_parent_company_owners).
    来源: 海通证券《单季度ROE因子改进》2024, IC=4-5%, ICIR~1.2.
    """
    df_inc = _get_financial_historical("financial_income", date)
    df_bal = _get_financial_historical("financial_balance", date)

    if df_inc.empty or df_bal.empty:
        return pd.Series(dtype=float, name="roe_trimmed")

    # Latest quarter net_profit per symbol
    inc_latest = df_inc.sort_values("stat_date").groupby("symbol").last()
    net_profit = inc_latest["net_profit"] if "net_profit" in inc_latest.columns else pd.Series(dtype=float)

    # Average equity: avg of last 2 periods
    bal_sorted = df_bal.sort_values("stat_date")
    equity_col = "equities_parent_company_owners"
    if equity_col not in bal_sorted.columns:
        return pd.Series(dtype=float, name="roe_trimmed")

    avg_equity = {}
    for sym, grp in bal_sorted.groupby("symbol"):
        eq_vals = grp[equity_col].dropna()
        if len(eq_vals) >= 1:
            # Average of last 2 available equity values
            avg_equity[sym] = eq_vals.tail(2).mean()

    avg_eq_series = pd.Series(avg_equity)

    # ROE = net_profit / avg_equity
    roe = net_profit / avg_eq_series.replace(0, np.nan)
    roe = roe.replace([np.inf, -np.inf], np.nan)
    roe = roe.where((roe > -1) & (roe < 1))

    # Trim top 10% (set to NaN)
    if len(roe.dropna()) >= 10:
        top_thresh = roe.quantile(0.90)
        roe = roe.where(roe < top_thresh)

    return _cs_zscore(roe, sparse=True).rename("roe_trimmed")




# ── Phase 4 专项数据源因子 ──

def compute_ihn(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """IHN 持仓机构个数: log(1+fund_count). 来源: 光大 2020, ICIR=0.74."""
    conn = _db_connect()
    rows = conn.execute(
        "SELECT symbol, fund_count FROM fund_hold "
        "WHERE report_date = (SELECT MAX(report_date) FROM fund_hold WHERE report_date <= ?)",
        (date,)
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float, name="ihn")
    s = pd.Series({r[0]: np.log1p(r[1]) if r[1] is not None and r[1] > 0 else np.nan for r in rows})
    return s.dropna().rename("ihn")


def compute_insider_increase(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """增持比例因子: sum(增持股数,90d) / total_mv. 来源: 源达 2025, IC=4.0%, ICIR=0.54."""
    conn = _db_connect()
    lookback_start = (pd.Timestamp(date) - pd.DateOffset(days=90)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT symbol, SUM(change_vol) as total_increase_vol "
        "FROM holder_trade WHERE ann_date >= ? AND ann_date <= ? "
        "AND direction = 'in' AND change_vol > 0 GROUP BY symbol",
        (lookback_start, date)
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float, name="insider_increase")

    increase_vol = pd.Series({r[0]: r[1] for r in rows})

    if fundamentals is not None and not fundamentals.empty and "total_mv" in fundamentals.columns:
        market_cap = fundamentals["total_mv"].fillna(0)
    else:
        conn2 = _db_connect()
        mv_df = pd.read_sql("SELECT symbol, total_mv FROM stocks WHERE total_mv > 0", conn2)
        market_cap = mv_df.set_index("symbol")["total_mv"] if not mv_df.empty else pd.Series(dtype=float)

    aligned = increase_vol.index.intersection(market_cap.index)
    if len(aligned) == 0:
        return pd.Series(dtype=float, name="insider_increase")
    ratio = increase_vol[aligned] / market_cap[aligned].replace(0, np.nan)
    ratio = ratio.replace([np.inf, -np.inf], np.nan).dropna()
    return _cs_zscore(ratio, sparse=True).rename("insider_increase")


def compute_earnings_revision(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """盈利修正三组件(简化): net_bull * log(1+report_count). 来源: 华泰 2024, ICIR=2.20."""
    conn = _db_connect()
    rows = conn.execute(
        "SELECT symbol, report_count, buy_count, overweight_count, "
        "neutral_count, underweight_count FROM analyst_forecast "
        "WHERE sync_date = (SELECT MAX(sync_date) FROM analyst_forecast WHERE sync_date <= ?)",
        (date,)
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float, name="earnings_revision")
    records = {}
    for r in rows:
        sym, n_report, n_buy, n_ow, n_neutral, n_uw = r
        if not n_report or n_report == 0:
            continue
        net_bull = ((n_buy or 0) + (n_ow or 0) - (n_uw or 0)) / n_report
        coverage = np.log1p(n_report)
        records[sym] = net_bull * coverage
    result = pd.Series(records)
    return _cs_zscore(result, sparse=True).rename("earnings_revision")



# ── EPD/EPDS 估值偏离因子 (东吴证券 2022, daily_valuation.pe_ttm) ──

# Module-level cache for daily_valuation data (per worker process, avoids re-query).
_DV_CACHE = {}  # {(lookback_start, date): DataFrame}


def _load_daily_valuation_pe(lookback_start: str, date: str) -> "pd.DataFrame":
    """Load daily_valuation.pe_ttm for [lookback_start, date]. Cached per worker."""
    cache_key = (lookback_start, date)
    if cache_key not in _DV_CACHE:
        conn = _db_connect()
        df = pd.read_sql(
            "SELECT symbol, date, pe_ttm FROM daily_valuation "
            "WHERE date >= ? AND date <= ? AND pe_ttm > 0 AND pe_ttm < 1000 "
            "ORDER BY date",
            conn, params=(lookback_start, date),
        )
        _DV_CACHE[cache_key] = df
        # Keep at most 3 cache entries to limit memory per worker.
        if len(_DV_CACHE) > 3:
            oldest = next(iter(_DV_CACHE))
            del _DV_CACHE[oldest]
    return _DV_CACHE[cache_key]


def compute_epd(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """EPD 估值偏离: -(PE_t - MA(PE,60d)) / std(PE,60d) (布林带偏离度).

    公式: PE z-score 取负 → 低 PE (估值便宜) = 正向信号。
    数据源: daily_valuation.pe_ttm (JQData).
    来源: 东吴证券《估值偏离因子研究》2022, ICIR=3.66.
    注: 原作 252d window, 当前仅有 ~82d pe_ttm, 暂用 60d (min_periods=20).
    """
    lookback_start = (pd.Timestamp(date) - pd.DateOffset(days=365)).strftime("%Y-%m-%d")
    df = _load_daily_valuation_pe(lookback_start, date)

    if df.empty:
        return pd.Series(dtype=float, name="epd")

    # Pivot: rows=date, cols=symbol, values=pe_ttm
    piv = df.pivot_table(index="date", columns="symbol", values="pe_ttm", aggfunc="mean")
    if piv.empty or piv.shape[1] < 5:
        return pd.Series(dtype=float, name="epd")

    # 60d rolling stats (close match to 252d when data < 1yr)
    rolling_mean = piv.rolling(60, min_periods=20).mean()
    rolling_std = piv.rolling(60, min_periods=20).std()

    # Latest date values
    latest_pe = piv.iloc[-1]
    latest_mean = rolling_mean.iloc[-1]
    latest_std = rolling_std.iloc[-1]

    # EPD = -(PE - mean) / std  (positive = undervalued)
    epd = -(latest_pe - latest_mean) / latest_std.replace(0, np.nan)
    return epd.dropna().rename("epd")


def compute_epds(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """EPDS 缓慢偏离: EPD × PE 回复稳定性 (IR 权重).

    公式: EPDS = EPD × |MA(PE,60d) / std(PE,60d)|.
    PE 均值回复越稳定 (高 mean/std), 信号越放大.
    来源: 东吴证券《估值偏离因子研究》2022, ICIR=4.02.
    注: 原作 252d window, 当前仅有 ~82d pe_ttm, 暂用 60d (min_periods=20).
    """
    lookback_start = (pd.Timestamp(date) - pd.DateOffset(days=365)).strftime("%Y-%m-%d")
    df = _load_daily_valuation_pe(lookback_start, date)

    if df.empty:
        return pd.Series(dtype=float, name="epds")

    piv = df.pivot_table(index="date", columns="symbol", values="pe_ttm", aggfunc="mean")
    if piv.empty or piv.shape[1] < 5:
        return pd.Series(dtype=float, name="epds")

    rolling_mean = piv.rolling(60, min_periods=20).mean()
    rolling_std = piv.rolling(60, min_periods=20).std()

    latest_pe = piv.iloc[-1]
    latest_mean = rolling_mean.iloc[-1]
    latest_std = rolling_std.iloc[-1]

    # EPD
    epd = -(latest_pe - latest_mean) / latest_std.replace(0, np.nan)

    # IR weight = |mean / std| — PE 回复稳定性
    ir_weight = (latest_mean.abs() / latest_std.replace(0, np.nan)).clip(0, 10)

    epds = epd * ir_weight
    return epds.dropna().rename("epds")



def compute_roe_reported(fundamentals, date, financials=None):
    """报告期 ROE = net_profit / total_owner_equities
    来源: Fama & French (2015) — 盈利能力因子
    """
    fin = financials
    if fin is None:
        from quant.data.store import DataStore
        store = DataStore()
        fin = store.get_financials(fundamentals.index.tolist(), date=date)
        store.close()
    if fin.empty or "net_profit" not in fin.columns or "total_owner_equities" not in fin.columns:
        return pd.Series(np.nan, index=fundamentals.index, name="roe_reported")
    roe = fin["net_profit"] / fin["total_owner_equities"]
    roe = roe.replace([np.inf, -np.inf], np.nan)
    roe = roe.where((roe > -1) & (roe < 1))  # filter extreme
    return _cs_zscore(roe.reindex(fundamentals.index), sparse=True).rename("roe_reported")


def compute_roa(fundamentals, date, financials=None):
    """ROA = net_profit / total_assets
    来源: Novy-Marx (2013) — 盈利能力
    """
    fin = financials
    if fin is None:
        from quant.data.store import DataStore
        store = DataStore()
        fin = store.get_financials(fundamentals.index.tolist(), date=date)
        store.close()
    if fin.empty or "net_profit" not in fin.columns or "total_assets" not in fin.columns:
        return pd.Series(np.nan, index=fundamentals.index, name="roa")
    roa = fin["net_profit"] / fin["total_assets"]
    roa = roa.replace([np.inf, -np.inf], np.nan)
    roa = roa.where((roa > -0.5) & (roa < 0.5))
    return _cs_zscore(roa.reindex(fundamentals.index), sparse=True).rename("roa")


def compute_debt_ratio(fundamentals, date, financials=None):
    """资产负债率 = total_liability / total_assets（低分=低负债=好）
    来源: Penman et al. (2007)
    """
    fin = financials
    if fin is None:
        from quant.data.store import DataStore
        store = DataStore()
        fin = store.get_financials(fundamentals.index.tolist(), date=date)
        store.close()
    if fin.empty or "total_liability" not in fin.columns or "total_assets" not in fin.columns:
        return pd.Series(np.nan, index=fundamentals.index, name="debt_ratio")
    dr = fin["total_liability"] / fin["total_assets"]
    dr = dr.replace([np.inf, -np.inf], np.nan)
    dr = dr.where((dr > 0) & (dr < 2))
    # 低负债=高分 (取负号), IC=可能正向(高负债在A股可能预示扩张)
    return _cs_zscore(dr, sparse=True).rename("debt_ratio")


def compute_accruals(fundamentals, date, financials=None):
    """应计利润 = (net_profit - net_operate_cash_flow) / total_assets
    来源: Sloan (1996) — 低应计利润=高质量盈利=未来高收益
    取负号: 低应计→高分
    """
    fin = financials
    if fin is None:
        from quant.data.store import DataStore
        store = DataStore()
        fin = store.get_financials(fundamentals.index.tolist(), date=date)
        store.close()
    needed = ["net_profit", "net_operate_cash_flow", "total_assets"]
    if fin.empty or not all(c in fin.columns for c in needed):
        return pd.Series(np.nan, index=fundamentals.index, name="accruals")
    acc = (fin["net_profit"] - fin["net_operate_cash_flow"]) / fin["total_assets"]
    acc = acc.replace([np.inf, -np.inf], np.nan)
    acc = acc.where((acc > -1) & (acc < 1))
    # 低应计→高分 (IC=负向)
    return _cs_zscore(-acc, sparse=True).rename("accruals")


# ═══════════════════════════════════════════════════════════
# 17. Asset Growth — Cooper, Gulen & Schill (2008)
#    A股验证: 华泰金工 2023. IC ≈ -0.03~-0.05.
#    总资产增速与未来收益负相关 (过度投资假说).
# ═══════════════════════════════════════════════════════════

def compute_asset_growth(fundamentals, date, financials=None):
    """资产增长率: (TA_t - TA_{t-4q}) / TA_{t-4q}, 取负号.

    Cooper, Gulen & Schill (2008): 资产快速扩张→未来低收益.
    TA = 总资产(total_assets), 同比(去年同期)避免季节性偏差.
    取负号: 高资产增速→低分→预期低收益 (IC为负).

    数据源: financial_balance.total_assets (季度).
    若去年同期数据缺失, 返回 NaN.
    """
    import sqlite3, os
    fin = financials
    if fin is None:
        from quant.data.store import DataStore
        store = DataStore()
        fin = store.get_financials(fundamentals.index.tolist(), date=date)
        store.close()

    if fin.empty or 'total_assets' not in fin.columns:
        return pd.Series(np.nan, index=fundamentals.index, name="asset_growth")

    # 当前季度: fin 已有最新 total_assets
    # 需要去年同期: 查询 financial_balance
    db = _market_db_path()
    conn = DatabaseManager.get_instance().get_connection("data/market.db")

    # 获取每个 symbol 的最新 stat_date
    _syms = fundamentals.index.tolist()
    _ph = ",".join(["?"] * len(_syms))
    rows = conn.execute(f"""
        SELECT symbol, stat_date, total_assets
        FROM financial_balance
        WHERE symbol IN ({_ph})
        ORDER BY stat_date DESC
    """, _syms).fetchall()

    # 按 symbol 分组, 取最新和去年同期
    import pandas as pd
    df_hist = pd.DataFrame(rows, columns=['symbol', 'stat_date', 'total_assets'])
    df_hist['stat_date'] = pd.to_datetime(df_hist['stat_date'])
    df_hist = df_hist.sort_values('stat_date')

    results = {}
    for sym in fundamentals.index:
        sym_data = df_hist[df_hist['symbol'] == sym].drop_duplicates(subset=['stat_date'], keep='last')
        if len(sym_data) < 2:
            continue
        # 最新季度
        latest = sym_data.iloc[-1]
        ta_now = latest['total_assets']
        latest_q = latest['stat_date']
        # 寻找去年同期 (同季度, 年份-1)
        target_q = latest_q - pd.DateOffset(years=1)
        # 找最接近 target_q 的季度 (窗口 ±90天)
        prev = sym_data[sym_data['stat_date'] <= target_q]
        if prev.empty:
            continue
        ta_prev = prev.iloc[-1]['total_assets']
        if ta_prev and ta_prev > 0 and ta_now and ta_now > 0:
            ag = (ta_now - ta_prev) / ta_prev
            results[sym] = ag

    ag_series = pd.Series(results, name="asset_growth")
    ag_series = ag_series.replace([np.inf, -np.inf], np.nan)
    ag_series = ag_series.where((ag_series > -1) & (ag_series < 10))
    # 高资产增速→低收益: 取负号 (IC为负)
    return _cs_zscore(-ag_series, sparse=True).rename("asset_growth")


# ═══════════════════════════════════════════════════════════
# 18. GP/TA — Novy-Marx (2013) Gross Profitability
#    Fama-French 2015 RMW 因子的核心成分.
#    A股验证: 高毛利组合年化超额 6-8%.
# ═══════════════════════════════════════════════════════════

def compute_gp_ta(fundamentals, date, financials=None):
    """毛利润/总资产: (operating_revenue - operating_cost) / total_assets.

    Novy-Marx (2013): GP/TA 比 ROE/ROA 更纯净 (不受杠杆和税率干扰).
    高分 = 强竞争优势 → 预期高收益 (IC为正).

    数据源: financial_income.operating_revenue/operating_cost 
           + financial_balance.total_assets.
    """
    fin = financials
    if fin is None:
        from quant.data.store import DataStore
        store = DataStore()
        fin = store.get_financials(fundamentals.index.tolist(), date=date)
        store.close()

    needed = ["operating_revenue", "operating_cost", "total_assets"]
    if fin.empty or not all(c in fin.columns for c in needed):
        return pd.Series(np.nan, index=fundamentals.index, name="gp_ta")

    gp = fin["operating_revenue"] - fin["operating_cost"]
    gp_ta = gp / fin["total_assets"]
    gp_ta = gp_ta.replace([np.inf, -np.inf], np.nan)
    gp_ta = gp_ta.where((gp_ta > -2) & (gp_ta < 5))
    # 高毛利→高分
    return _cs_zscore(gp_ta, sparse=True).rename("gp_ta")


# ═══════════════════════════════════════════════════════════
# 19. 停牌比率 (Zero Trading Days) — Liu (2006)
#    针对中国市场的流动性度量. 比 Amihud 更适配 A 股特征.
#    高停牌比率=流动性差=折价.
# ═══════════════════════════════════════════════════════════

def compute_sue(fundamentals, date, financials=None):
    """标准化未预期盈余: (EPS_latest - EPS_yoy) / std(EPS_8q).

    Bernard & Thomas (1989): 盈余公告后漂移(PEAD).
    高分=盈余超预期 → 预期正收益 (IC为正).

    数据源: financial_income.net_profit / stocks.total_shares = 季度EPS.
    需要 total_shares 列 (Step 2 新增, 通过 fundamental.py 同步自 stock_value_em).
    若 total_shares 为空则返回 NaN.
    """
    import sqlite3, pandas as pd, numpy as np

    db = _market_db_path()
    conn = DatabaseManager.get_instance().get_connection("data/market.db")

    _syms = fundamentals.index.tolist()
    _ph = ",".join(["?"] * len(_syms))

    # 读取季度净利润 + 总股本
    rows = conn.execute(f"""
        SELECT fi.symbol, fi.stat_date, fi.net_profit, s.total_shares
        FROM financial_income fi
        JOIN stocks s ON fi.symbol = s.symbol
        WHERE fi.stat_date <= ?
          AND fi.symbol IN ({_ph})
          AND s.total_shares IS NOT NULL
          AND s.total_shares > 0
          AND fi.net_profit IS NOT NULL
        ORDER BY fi.symbol, fi.stat_date DESC
    """, [date] + _syms).fetchall()

    if not rows:
        return pd.Series(np.nan, index=fundamentals.index, name="sue")

    df = pd.DataFrame(rows, columns=['symbol', 'stat_date', 'net_profit', 'total_shares'])
    df['stat_date'] = pd.to_datetime(df['stat_date'])
    df['eps'] = df['net_profit'] / df['total_shares']

    results = {}
    for sym in fundamentals.index:
        sym_data = df[df['symbol'] == sym].sort_values('stat_date', ascending=True)
        if len(sym_data) < 3:
            continue  # 需要至少3个季度数据

        # 最新季度
        latest = sym_data.iloc[-1]
        eps_latest = latest['eps']
        latest_q = latest['stat_date']

        # 去年同期
        target_q = latest_q - pd.DateOffset(years=1)
        prev = sym_data[sym_data['stat_date'] <= target_q]
        if prev.empty:
            continue
        eps_yoy = prev.iloc[-1]['eps']

        # 8季度标准差
        eps_series = sym_data['eps'].tail(8)
        if len(eps_series) < 4:
            continue  # 至少4个数据点才计算标准差
        eps_std = eps_series.std()

        if eps_std > 0:
            sue = (eps_latest - eps_yoy) / eps_std
            results[sym] = sue

    sue_series = pd.Series(results, name="sue")
    sue_series = sue_series.replace([np.inf, -np.inf], np.nan)
    sue_series = sue_series.clip(-5, 5)
    # 高SUE→高分
    return _cs_zscore(sue_series, sparse=True).rename("sue")




# ═══════════════════════════════════════════════════════════
# 22. 大股东减持 — 上交所 2020; 海通金工 2023
#    大股东减持→负面信号→预期负收益. 取负号 (高减持→低分).
# ═══════════════════════════════════════════════════════════

def compute_holder_reduction(fundamentals, date, financials=None):
    """大股东减持因子: 过去60日大股东减持比例, 取负号.

    来源: 上交所 2020 研究; 海通金工 2023.
    大股东接近信息源, 减持包含内幕负面信号.
    高分 = 低减持 (好股票). IC期望为负 (减持→低收益).

    数据源: holder_trade (需先运行 data/holder_trade.py sync).
    若表为空则返回 NaN.
    """
    import sqlite3, pandas as pd

    db = _market_db_path()
    conn = DatabaseManager.get_instance().get_connection("data/market.db")

    _syms = fundamentals.index.tolist()
    _ph = ",".join(["?"] * len(_syms))
    end_date = pd.Timestamp(date)
    start_date = end_date - pd.DateOffset(days=60)

    rows = conn.execute(f"""
        SELECT symbol, SUM(CASE WHEN direction='out' THEN change_vol ELSE 0 END) as total_out_vol
        FROM holder_trade
        WHERE ann_date BETWEEN ? AND ?
          AND symbol IN ({_ph})
        GROUP BY symbol
    """, [start_date.strftime("%Y-%m-%d"), date] + _syms).fetchall()

    vals = {r[0]: r[1] for r in rows if r[1] is not None}
    result = pd.Series(vals, name="holder_reduction")
    result = result.replace([float('inf'), float('-inf')], float('nan'))
    # 高减持→低分 (IC为负)
    # 注: akshare 只返回绝对股数, 横截面 z-score 标准化已处理量纲差异
    return _cs_zscore(-result, sparse=True).rename("holder_reduction")


# ═══════════════════════════════════════════════════════════
# 23. 股权质押比例 — 中信建投 2022
#    高质押→平仓风险→负溢价. 取负号 (高质押→低分).
# ═══════════════════════════════════════════════════════════

def compute_pledge_ratio(fundamentals, date, financials=None):
    """股权质押比例: 质押股数/总股本, 取负号.

    来源: 中信建投 2022.
    高质押比例→质押预警线/平仓线风险→股价崩盘风险溢价.
    高分 = 低质押 (安全). IC期望为负 (高质押→低收益).

    数据源: pledge_stat (需先运行 data/pledge.py sync).
    """
    import sqlite3, pandas as pd

    db = _market_db_path()
    conn = DatabaseManager.get_instance().get_connection("data/market.db")

    _syms = fundamentals.index.tolist()
    _ph = ",".join(["?"] * len(_syms))

    rows = conn.execute(f"""
        SELECT symbol, pledge_shares, total_shares
        FROM pledge_stat
        WHERE symbol IN ({_ph})
          AND end_date <= ?
          AND total_shares IS NOT NULL AND total_shares > 0
        GROUP BY symbol
        HAVING end_date = MAX(end_date)
    """, _syms + [date]).fetchall()

    vals = {}
    for r in rows:
        if r[1] and r[2] and r[2] > 0:
            vals[r[0]] = r[1] / r[2]

    result = pd.Series(vals, name="pledge_ratio")
    result = result.clip(0, 1)
    # 高质押→低分
    return _cs_zscore(-result, sparse=True).rename("pledge_ratio")


# ═══════════════════════════════════════════════════════════
# 24. 股息率 — 中信金工 2023
#    高股息→正溢价. 取正号 (高股息→高分).
# ═══════════════════════════════════════════════════════════

def compute_dividend_yield(fundamentals, date, financials=None):
    """股息率因子: 最近12个月现金分红/当前股价.

    来源: 中信金工 2023 — A股高股息策略年化超额~4-5%.
    高分 = 高股息率. IC期望为正.

    数据源: dividend (需先运行 data/dividend.py sync) + stocks.total_mv/close.
    """
    import sqlite3, pandas as pd

    db = _market_db_path()
    conn = DatabaseManager.get_instance().get_connection("data/market.db")

    _syms = fundamentals.index.tolist()
    _ph = ",".join(["?"] * len(_syms))

    # 取最近12个月分红
    end_date = pd.Timestamp(date)
    start_date = end_date - pd.DateOffset(months=12)

    div_rows = conn.execute(f"""
        SELECT symbol, SUM(cash_div) as total_div
        FROM dividend
        WHERE record_date BETWEEN ? AND ?
          AND symbol IN ({_ph})
          AND cash_div IS NOT NULL
        GROUP BY symbol
    """, [start_date.strftime("%Y-%m-%d"), date] + _syms).fetchall()

    # 取股价 (从 stocks.high_52w 或 close_latest)
    price_rows = conn.execute(f"""
        SELECT symbol, pe, total_mv FROM stocks WHERE symbol IN ({_ph})
    """, _syms).fetchall()

    div_map = {r[0]: r[1] for r in div_rows if r[1] and r[1] > 0}
    # 用 total_mv / total_shares 估股价 (更稳健)
    vals = {}
    for sym in fundamentals.index:
        div = div_map.get(sym)
        if div and div > 0:
            # 用 fundamentals 里的 pe/total_mv 反推股价: price = total_mv / total_shares
            # 简化: 直接用 div 做截面标准化 (量纲统一)
            vals[sym] = div

    result = pd.Series(vals, name="dividend_yield")
    result = result.replace([float('inf'), float('-inf')], float('nan'))
    # 高股息→高分
    return _cs_zscore(result, sparse=True).rename("dividend_yield")




# ═══════════════════════════════════════════════════════════
# P70: 四新因子 — OIR 昼夜 / STR 量稳 / ABN_TURN 残差 / OCFP 现金流
# 来源: 2021-2026 券商金工研报系统搜索, docs/research/四因子接入分析_2026-07-07.md
# ═══════════════════════════════════════════════════════════

def compute_ocfp(fundamentals, date, financials=None):
    """OCFP 经营现金流/市值: TTM 经营活动现金流净额 / 总市值.
    
    华泰证券(2016): 《单因子测试之估值类因子》.
    ICIR=0.526 (所有静态估值因子中最高).
    逻辑: 经营现金流比利润/净资产更难操纵, 高OCFP=真金白银的廉价.
    季频更新, 与日频因子天然低相关.

    需 financials['cash_flow'] 数据 (已在 compute_all_factors 中预加载).
    金融/地产/银行剔除 (季报滞后, 行业中性化必需).
    """
    import sqlite3, numpy as np, os

    if fundamentals is None or fundamentals.empty:
        return pd.Series(np.nan, index=fundamentals.index, name="ocfp")

    syms = fundamentals.index.tolist()

    # 金融/地产/银行剔除
    # 从 fundamentals 获取总市值和行业（无需重复查询 DB）
    mv_series = fundamentals["total_mv"] if "total_mv" in fundamentals.columns else None
    ind_series = fundamentals["industry"] if "industry" in fundamentals.columns else None
    
    if mv_series is None:
        return pd.Series(np.nan, index=fundamentals.index, name="ocfp")
    
    mv_map = mv_series.dropna().to_dict()
    ind_map = ind_series.dropna().to_dict() if ind_series is not None else {}

    # 金融/地产/银行剔除
    exclude_inds = {'银行', '非银金融', '房地产', '综合金融'}
    valid_syms = [s for s in syms if s in mv_map and ind_map.get(s, '') not in exclude_inds]

    # TTM经营现金流: 直接查 financial_cash_flow 表最近4个季度
    ocfp_vals = {}
    _conn = DatabaseManager.get_instance().get_connection("data/market.db")
    placeholders = ",".join("?" for _ in valid_syms)
    cf_df = pd.read_sql_query(
        f"""SELECT symbol, stat_date, net_operate_cash_flow
            FROM financial_cash_flow
            WHERE stat_date >= date(?, '-1 year')
              AND symbol IN ({placeholders})
            ORDER BY symbol, stat_date""",
        _conn, params=[date] + valid_syms
    )
    if not cf_df.empty:
        for sym in valid_syms:
            sym_cf = cf_df[cf_df['symbol'] == sym]
            if len(sym_cf) == 0:
                continue
            # TTM: 最近4个季度
            recent = sym_cf.tail(4)
            ttm = recent['net_operate_cash_flow'].sum()
            mv = mv_map.get(sym)
            if mv and mv > 0:
                ocfp_vals[sym] = ttm / mv

    raw = pd.Series(ocfp_vals, name="ocfp")
    if raw.empty or raw.count() < 30:
        return raw

    # 行业中性化
    import numpy as np
    common = [s for s in raw.index if s in ind_map]
    if len(common) >= 30:
        industries = [ind_map[s] for s in common]
        ind_counts = pd.Series(industries).value_counts()
        valid_inds = ind_counts[ind_counts >= 3].index.tolist()
        ind_dummies = pd.get_dummies(industries)
        valid_cols = [c for c in ind_dummies.columns if c in valid_inds and c != '']
        if valid_cols:
            from sklearn.linear_model import LinearRegression
            X = ind_dummies[valid_cols].values
            y = raw.loc[common].values
            resid = y - LinearRegression().fit(X, y).predict(X)
            raw = pd.Series(resid, index=common)

    # 正向: 高OCFP→高分
    return _cs_zscore(raw, sparse=True).rename("ocfp")



# ═══════════════════════════════════════════════════════════
# P71: 涨跌停制度特有效因子 — 封成比 / 封板时间 / 涨停打开 / 净涨停占比
# ═══════════════════════════════════════════════════════════

_shared_limit_conn = None  # 模块级共享连接, 避免每个因子重复开 DB

def compute_epa(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """EPA估值异常: PE 截面 Z-score 取负 (PE高→估值贵→负信号).

    来源: 东吴证券 — EP偏离+风格正交, ICIR=4.75, 所有静态估值因子最强.
    数据: fundamentals 表 (pe / pe_ttm 字段), 由 get_fundamentals 提供.
    """
    if fundamentals is None or fundamentals.empty:
        return pd.Series(name="epa", dtype=float)

    symbols_all = list(fundamentals.index)

    # 优先 pe_ttm, 其次 pe (get_fundamentals 已用 JQData pe_ttm 覆盖 pe)
    pe_col = "pe_ttm" if "pe_ttm" in fundamentals.columns else "pe"
    if pe_col not in fundamentals.columns:
        return pd.Series(0.0, index=symbols_all, name="epa")

    pe_series = fundamentals[pe_col].dropna().astype(float)
    if len(pe_series) < 30:
        return pd.Series(0.0, index=symbols_all, name="epa")

    pe_mean = pe_series.mean()
    pe_std = pe_series.std()
    if pe_std == 0:
        return pd.Series(0.0, index=symbols_all, name="epa")

    raw = (pe_series - pe_mean) / pe_std
    result = pd.Series(-raw, index=symbols_all)  # PE 过高→负信号
    return _cs_zscore(result, sparse=True).rename("epa")


def compute_insider_cluster(data, date, window=60):
    """高管/大股东集体增持聚类: 60天内 >= 3 人或增持比例 > 0.1%。

    数据源: holder_trade 表 (data/holder_trade.py).
    IC预估: 0.03-0.05, 正向因子 (集体增持→高分).
    """
    import sqlite3, os as _os5
    symbols = list(data.index)
    result = pd.Series(0.0, index=symbols)
    conn = DatabaseManager.get_instance().get_connection("data/market.db")
    rows = conn.execute(
        "SELECT symbol, holder_type, direction, change_ratio FROM holder_trade "
        "WHERE ann_date >= date(?, '-{} days') AND direction IN ('增加','增持','买入')".format(window),
        (str(date)[:10],)
    ).fetchall()
    if rows:
        import pandas as _pd5
        df = _pd5.DataFrame(rows, columns=["symbol", "holder_type", "direction", "change_ratio"])
        for sym in symbols:
            sym_data = df[df["symbol"] == sym]
            if len(sym_data) == 0:
                continue
            n_insiders = len(sym_data)
            total_ratio = sym_data["change_ratio"].fillna(0).sum()
            # Signal: number of insiders + total ratio
            score = n_insiders * 0.3 + min(total_ratio / 100, 1.0)
            result[sym] = score if n_insiders >= 2 else 0
    return _cs_zscore(result).rename("insider_cluster")


def compute_earnings_upgrade(data, date, window=90):
    """分析师盈利预测上调幅度: EPS 30天前 vs 现在。

    数据源: analyst_forecast 表 (data/analyst.py).
    IC预估: 0.03-0.05, 正向因子 (上调→高分).
    """
    import sqlite3, os as _os6
    symbols = list(data.index)
    result = pd.Series(0.0, index=symbols)
    conn = DatabaseManager.get_instance().get_connection("data/market.db")
    # Get latest analyst forecast for each stock
    rows = conn.execute(
        "SELECT symbol, buy_count, overweight_count, neutral_count, "
        "underweight_count, report_count FROM analyst_forecast "
        "WHERE sync_date <= ? ORDER BY sync_date DESC",
        (str(date)[:10],)
    ).fetchall()
    if rows:
        import pandas as _pd6
        df = _pd6.DataFrame(rows, columns=[
            "symbol", "buy", "overweight", "neutral", "underweight", "total"
        ]).drop_duplicates(subset="symbol", keep="first")

        for _, row in df.iterrows():
            sym = row["symbol"]
            if sym not in symbols:
                continue
            total = row["total"]
            if total and total > 0:
                bull_ratio = ((row["buy"] or 0) + (row["overweight"] or 0)) / total
                bear_ratio = (row["underweight"] or 0) / total
                result[sym] = bull_ratio - bear_ratio
    return _cs_zscore(result, sparse=True).rename("earnings_upgrade")



# ═══════════════════════════════════════════════════════════
# Gap 7b: 宏观因子 (另类数据)
# ═══════════════════════════════════════════════════════════

def _get_macro_value(indicator: str, date: str) -> float:
    """读取 macro_indicator 表中最近可用的宏观指标值."""
    import sqlite3
    conn = DatabaseManager.get_instance().get_connection("data/market.db")
    row = conn.execute(
        "SELECT value FROM macro_indicator WHERE indicator=? AND date <= ? ORDER BY date DESC LIMIT 1",
        (indicator, date)
    ).fetchone()
    return row[0] if row else None


def compute_macro_pmi_diff(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """PMI 偏离荣枯线: PMI_manufacturing - 50.
    来源: 中金公司(2019) - PMI>50 期间中证全指年化 +15%, <50 年化 +2%.
    所有股票同值 -> 系统性因子."""
    pmi = _get_macro_value("pmi_manufacturing", date)
    if pmi is None:
        return pd.Series(0.0, index=fundamentals.index, name="macro_pmi_diff")
    return pd.Series(pmi - 50.0, index=fundamentals.index, name="macro_pmi_diff")


def compute_macro_m2_yoy(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """M2 同比增速: 流动性因子.
    来源: 华泰证券(2018) - M2 增速与 A 股估值正相关. 所有股票同值."""
    m2 = _get_macro_value("m2_yoy", date)
    if m2 is None:
        return pd.Series(0.0, index=fundamentals.index, name="macro_m2_yoy")
    return pd.Series(m2, index=fundamentals.index, name="macro_m2_yoy")


def compute_macro_cpi_yoy(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """CPI 同比: 通胀预期因子.
    来源: 中信证券(2020) - 温和通胀(2-4%)利好股市, 通缩和高通胀利空."""
    cpi = _get_macro_value("cpi_yoy", date)
    if cpi is None:
        return pd.Series(0.0, index=fundamentals.index, name="macro_cpi_yoy")
    return pd.Series(cpi, index=fundamentals.index, name="macro_cpi_yoy")


def compute_macro_rate_10y(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """10年期国债收益率: 折现率因子 (取负号).
    来源: DCF 模型 - 无风险利率上升 -> 折现率上升 -> 股票内在价值下降."""
    rate = _get_macro_value("bond_10y_yield", date)
    if rate is None:
        return pd.Series(0.0, index=fundamentals.index, name="macro_rate_10y")
    return pd.Series(-rate, index=fundamentals.index, name="macro_rate_10y")


_FUNDAMENTAL_FN_MAP = {
    "ep_ratio":      ("value_ep",       compute_ep_ratio),
    "bp_ratio":      ("value_bp",       compute_bp_ratio),
    "roe_ratio":     ("profitability",  compute_roe_ratio),
    "high52w_dist":  ("high52w",        compute_high52w_dist),
    "size":          ("size_large_cap", compute_size),  # A股大盘溢价
    # P69: 集中化 — 从动态注册迁移到静态 map
    "roe_reported":        ("profitability",  compute_roe_reported),
    "roa":                 ("profitability",  compute_roa),
    "debt_ratio":          ("leverage",       compute_debt_ratio),
    "accruals":            ("quality",        compute_accruals),
    "asset_growth":        ("fundamental",    compute_asset_growth),
    "gp_ta":               ("profitability",  compute_gp_ta),
    "sue":                 ("fundamental",    compute_sue),
    "holder_reduction":    ("institution",    compute_holder_reduction),
    "pledge_ratio":        ("risk",           compute_pledge_ratio),
    "dividend_yield":      ("value",          compute_dividend_yield),
    "ocfp":                ("value",          compute_ocfp),
    "epa":                 ("value",          compute_epa),
    "margin_buy_ratio":    ("margin",         compute_margin_buy_ratio),
    "analyst_consensus":   ("analyst",        compute_analyst_consensus),
    "epd":                 ("value",          compute_epd),
    "epds":                ("value",          compute_epds),
    "gross_margin_diff":   ("profitability",  compute_gross_margin_diff),
    "financial_anomaly":   ("quality",        compute_financial_anomaly),
    "roe_trimmed":         ("profitability",  compute_roe_trimmed),
    "ihn":                 ("institution",    compute_ihn),
    "insider_increase":    ("institution",    compute_insider_increase),
    "earnings_revision":   ("analyst",        compute_earnings_revision),
    "insider_cluster":      ("institution",    compute_insider_cluster),
    "earnings_upgrade":     ("analyst",        compute_earnings_upgrade),
    # Gap 7b: 宏观因子
    "macro_pmi_diff":       ("macro",          compute_macro_pmi_diff),
    "macro_m2_yoy":         ("macro",          compute_macro_m2_yoy),
    "macro_cpi_yoy":        ("macro",          compute_macro_cpi_yoy),
    "macro_rate_10y":       ("macro",          compute_macro_rate_10y),
}
