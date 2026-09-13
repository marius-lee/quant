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

from quant.factor.compute._fundamental_group2 import compute_debt_ratio, compute_accruals, compute_asset_growth, _asset_growth_from_rows, compute_gp_ta, compute_sue, _sue_from_rows, compute_holder_reduction, compute_pledge_ratio, compute_dividend_yield
from quant.factor.compute._fundamental_group1 import compute_ep_ratio, compute_bp_ratio, compute_size, compute_roe_ratio, compute_margin_buy_ratio, compute_analyst_consensus, _get_financial_historical, _ttm_sum, compute_gross_margin_diff
from quant.factor.compute._fundamental_group1 import compute_high52w_dist
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

from quant.utils.date import to_str
from quant.config.constants import *
from quant.factor.registry import _cs_zscore, _db_connect, _FIN_FACTORS
from quant.factor.compute._shared import _market_db_path
from quant.factor.compute.classic.value import (
    compute_alpha_ep, compute_alpha_bp, compute_alpha_sp, compute_alpha_cfp,
)  # v629: 迁移自 _PRICE_FN_MAP (旧接口 date_str,conn → 新接口 fundamentals,date)
from quant.factor.compute._pit import (
    pit_visible_mask, pit_where_sql, pit_where_params,
)
from quant.data.repos._base import DatabaseManager  # noqa: F401 (bw compat)
from quant.factor.compute.missing import compute_revenue_growth_yoy  # test-v323
from quant.factor.compute.missing import (
    compute_earnings_growth_yoy, compute_piotroski_fscore,
)  # test-v325
from quant.factor.compute.high_priority import compute_cf_roa  # v358

from quant.utils.logger import get_logger

logger = get_logger("factor.compute.fundamental")


def compute_financial_anomaly(fundamentals: "pd.DataFrame", date: str, aux=None) -> "pd.Series":
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
    # v523: aux 快路径 — chunk 级预载两表, 消除每日 2× 400k 行全表查询
    if aux is not None and "financial_income" in aux and "financial_balance" in aux:
        date_ts = pd.Timestamp(to_str(date)) if not isinstance(date, str) else pd.Timestamp(date)
        # P0 (2026-08-29): PIT 披露过滤替代 stat_date<=; aux 已由 slice_aux_for_date
        # 预先 PIT 切片, 此处再用 pit_visible_mask 兜底 (独立调用/单测亦正确).
        df_inc = aux["financial_income"][pit_visible_mask(aux["financial_income"], date)]
        df_bal = aux["financial_balance"][pit_visible_mask(aux["financial_balance"], date)]
        if df_inc.empty or df_bal.empty:
            return pd.Series(dtype=float, name="financial_anomaly")
    else:
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
    # v544: 子因子值为 NaN 时跳过 (不传染整只股票); 归一化用平均偏差 z/count,
    # 保证子因子可用数 (3/4) 不同时跨期口径一致 (原 z/count*4 在 count<4 时放大)
    scores = {}
    all_syms = set(rev_growth.index) | set(inv_growth.index) | set(ar_growth.index) | set(gm_change.index)
    for sym in all_syms:
        z = 0.0
        count = 0
        # 1. Inventories anomaly
        if sym in inv_growth.index and sym in rev_growth.index:
            inv_g, rev_g = inv_growth[sym], rev_growth[sym]
            if pd.notna(inv_g) and pd.notna(rev_g):
                z += -(inv_g - rev_g)
                count += 1
        # 2. Receivables anomaly
        if sym in ar_growth.index and sym in rev_growth.index:
            ar_g, rev_g = ar_growth[sym], rev_growth[sym]
            if pd.notna(ar_g) and pd.notna(rev_g):
                z += -(ar_g - rev_g)
                count += 1
        # 3. Admin expense anomaly
        if sym in admin_growth.index and sym in rev_growth.index:
            adm_g, rev_g = admin_growth[sym], rev_growth[sym]
            if pd.notna(adm_g) and pd.notna(rev_g):
                z += -(adm_g - rev_g)
                count += 1
        # 4. Gross margin change
        if sym in gm_change.index:
            gm_v = gm_change[sym]
            if pd.notna(gm_v):
                z += -gm_v
                count += 1
        if count > 0:
            scores[sym] = z / count

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
    conn.close()
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
        conn.close()
        return pd.Series(dtype=float, name="insider_increase")

    increase_vol = pd.Series({r[0]: r[1] for r in rows})

    if fundamentals is not None and not fundamentals.empty and "total_mv" in fundamentals.columns:
        market_cap = fundamentals["total_mv"].fillna(0)
    else:
        conn2 = _db_connect()
        mv_df = pd.read_sql("SELECT symbol, total_mv FROM stocks WHERE total_mv > 0", conn2)
        market_cap = mv_df.set_index("symbol")["total_mv"] if not mv_df.empty else pd.Series(dtype=float)
        conn.close()
        conn2.close()


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
        conn.close()
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
        conn.close()
    return _DV_CACHE[cache_key]


def compute_epd(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """EPD 估值偏离: -(PE_t - MA(PE,60d)) / std(PE,60d) (布林带偏离度).

    公式: PE z-score 取负 → 低 PE (估值便宜) = 正向信号。
    数据源: daily_valuation.pe_ttm (JQData).
    来源: 东吴证券《估值偏离因子研究》2022, ICIR=3.66.
    注: 原作 252d window, 当前仅有 ~82d pe_ttm, 暂用 60d (min_periods=20).
    """
    lookback_start = (pd.Timestamp(date) - pd.DateOffset(days=_require_cfg("data.lookback_days"))).strftime("%Y-%m-%d")
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
    lookback_start = (pd.Timestamp(date) - pd.DateOffset(days=_require_cfg("data.lookback_days"))).strftime("%Y-%m-%d")
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
    # v477: 原实现 date 直接作 SQL 参数 — dispatch 传 Timestamp 绑定异常 → 0 行
    date_str = to_str(date) if not isinstance(date, str) else date

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

    # TTM经营现金流: 直接查 financial_cashflow 表最近4个季度
    ocfp_vals = {}
    _conn = _db_connect()
    placeholders = ",".join("?" for _ in valid_syms)
    cf_df = pd.read_sql_query(
        f"""SELECT symbol, stat_date, net_operate_cash_flow
            FROM financial_cashflow
            WHERE stat_date >= date(?, '-1 year')
              AND ({pit_where_sql()})
              AND symbol IN ({placeholders})
            ORDER BY symbol, stat_date""",
        _conn, params=[date_str] + list(pit_where_params(date_str)) + valid_syms
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
    _conn.close()
    return _cs_zscore(raw, sparse=True).rename("ocfp")



# ═══════════════════════════════════════════════════════════
# P71: 涨跌停制度特有效因子 — 封成比 / 封板时间 / 涨停打开 / 净涨停占比
# ═══════════════════════════════════════════════════════════

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
    conn = _db_connect()
    rows = conn.execute(
        "SELECT symbol, holder_type, direction, change_ratio FROM holder_trade "
        "WHERE ann_date >= date(?, '-{} days') AND direction IN ('增加','增持','买入')".format(window),
        (to_str(date),)
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
    conn.close()
    return _cs_zscore(result).rename("insider_cluster")


def compute_earnings_upgrade(data, date, window=90):
    """分析师盈利预测上调幅度: EPS 30天前 vs 现在。

    数据源: analyst_forecast 表 (data/analyst.py).
    IC预估: 0.03-0.05, 正向因子 (上调→高分).
    """
    import sqlite3, os as _os6
    symbols = list(data.index)
    result = pd.Series(0.0, index=symbols)
    conn = _db_connect()
    # Get latest analyst forecast for each stock
    rows = conn.execute(
        "SELECT symbol, buy_count, overweight_count, neutral_count, "
        "underweight_count, report_count FROM analyst_forecast "
        "WHERE sync_date <= ? ORDER BY sync_date DESC",
        (to_str(date),)
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
    conn.close()
    return _cs_zscore(result, sparse=True).rename("earnings_upgrade")



# ═══════════════════════════════════════════════════════════
# Gap 7b: 宏观因子 (另类数据)
# ═══════════════════════════════════════════════════════════

def _get_macro_value(indicator: str, date: str) -> float:
    """读取 macro_indicator 表中最近可用的宏观指标值."""
    import sqlite3
    conn = _db_connect()
    row = conn.execute(
        "SELECT value FROM macro_indicator WHERE indicator=? AND date <= ? ORDER BY date DESC LIMIT 1",
        (indicator, date)
    ).fetchone()
    result = row[0] if row else None
    conn.close()
    return result


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
    # v323/v325/v358: 基本面因子 (data 兼容 MultiIndex + 简单 DataFrame)
    "revenue_growth_yoy":   ("fundamental",    compute_revenue_growth_yoy),
    "earnings_growth_yoy":  ("fundamental",    compute_earnings_growth_yoy),
    "piotroski_fscore":     ("fundamental",    compute_piotroski_fscore),
    "cf_roa":              ("fundamental",    compute_cf_roa),
    # v629: Alpha 价值因子迁入 (旧接口 date_str,conn 导致物化零结果)
    "alpha_ep":              ("value",          compute_alpha_ep),
    "alpha_bp":              ("value",          compute_alpha_bp),
    "alpha_sp":              ("value",          compute_alpha_sp),
    "alpha_cfp":             ("value",          compute_alpha_cfp),
}


