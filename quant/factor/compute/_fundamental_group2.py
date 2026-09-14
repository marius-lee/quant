"""Factor compute fundamental group 2."""

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


def compute_asset_growth(fundamentals, date, financials=None, aux=None):
    """资产增长率: (TA_t - TA_{t-4q}) / TA_{t-4q}, 取负号.

    Cooper, Gulen & Schill (2008): 资产快速扩张→未来低收益.
    TA = 总资产(total_assets), 同比(去年同期)避免季节性偏差.
    取负号: 高资产增速→低分→预期低收益 (IC为负).

    数据源: financial_balance.total_assets (季度).
    若去年同期数据缺失, 返回 NaN.
    """
    import sqlite3, os

    # v523: aux 快路径 — chunk 级预载 financial_balance, 消除每日裸 SQL 大查询
    # (IN 5208 全表扫, 物化实测单日 56s→~0.3s)
    if aux is not None and "financial_balance" in aux:
        fb = aux["financial_balance"]
        if not fb.empty and "total_assets" in fb.columns:
            date_ts = pd.Timestamp(to_str(date)) if not isinstance(date, str) else pd.Timestamp(date)
            # P0 (2026-08-29): PIT 披露过滤替代 stat_date<=
            sub = fb[pit_visible_mask(fb, date)].copy()
            if not sub.empty:
                sub["stat_date"] = pd.to_datetime(sub["stat_date"])
                results = _asset_growth_from_rows(sub.sort_values("stat_date"),
                                                  fundamentals.index)
                if results:
                    ag_series = pd.Series(results, name="asset_growth")
                    ag_series = ag_series.replace([np.inf, -np.inf], np.nan)
                    ag_series = ag_series.where((ag_series > -1) & (ag_series < 10))
                    return _cs_zscore(-ag_series, sparse=True).rename("asset_growth")
                return pd.Series(np.nan, index=fundamentals.index, name="asset_growth")

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
    conn = _db_connect()

    # 获取每个 symbol 的最新 stat_date
    _syms = fundamentals.index.tolist()
    _ph = ",".join(["?"] * len(_syms))
    rows = conn.execute(f"""
        SELECT symbol, stat_date, total_assets
        FROM financial_balance
        WHERE symbol IN ({_ph})
          AND ({pit_where_sql()})
        ORDER BY stat_date DESC
    """, tuple(_syms) + pit_where_params(date)).fetchall()

    # 按 symbol 分组, 取最新和去年同期
    df_hist = pd.DataFrame(rows, columns=['symbol', 'stat_date', 'total_assets'])
    df_hist['stat_date'] = pd.to_datetime(df_hist['stat_date'])
    df_hist = df_hist.sort_values('stat_date')

    results = _asset_growth_from_rows(df_hist, fundamentals.index)

    ag_series = pd.Series(results, name="asset_growth")
    ag_series = ag_series.replace([np.inf, -np.inf], np.nan)
    ag_series = ag_series.where((ag_series > -1) & (ag_series < 10))
    conn.close()
    # 高资产增速→低收益: 取负号 (IC为负)
    return _cs_zscore(-ag_series, sparse=True).rename("asset_growth")



def _asset_growth_from_rows(df_hist: "pd.DataFrame", symbols) -> dict:
    """按每股计算资产增长率 YoY (aux 快路径 + SQL fallback 共用)."""
    results = {}
    df_hist_idx = df_hist.set_index("symbol")
    for sym in symbols:
        if sym not in df_hist_idx.index:
            continue
        sym_data = df_hist_idx.loc[[sym]].drop_duplicates(subset=['stat_date'], keep='last')
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
    return results


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


def compute_sue(fundamentals, date, financials=None, aux=None):
    """标准化未预期盈余: (EPS_latest - EPS_yoy) / std(EPS_8q).

    Bernard & Thomas (1989): 盈余公告后漂移(PEAD).
    高分=盈余超预期 → 预期正收益 (IC为正).

    数据源: financial_income.net_profit / stocks.total_shares = 季度EPS.
    需要 total_shares 列 (Step 2 新增, 通过 fundamental.py 同步自 stock_value_em).
    若 total_shares 为空则返回 NaN.

    口径 (v523-A2/A2b 定论): aux 快路径窗口 = financial_lookback_days (1100d);
    次新股 (301/688/603 等新板) 的招股书期 EPS (股本小 → 超大离群值) 常在窗口外,
    tail(8) 只涵盖上市后连续报告 — 比 fallback 全史 SQL (含 IPO 前不可比期) 更符合
    SUE 定义. 两径横截面序一致 (RankIC>=0.97), 差异票均为次新股, 见
    test/test_factor_aux_consistency.py.
    """
    import sqlite3, pandas as pd, numpy as np

    # v523: aux 快路径 — chunk 级预载 financial_income + stocks.total_shares,
    # 消除每日裸 SQL 大查询 (IN 5208 + JOIN + 每股循环, 物化实测单日 16.7s→~0.3s)
    if aux is not None and "financial_income" in aux and "stocks" in aux:
        fi = aux["financial_income"]
        stk = aux["stocks"]
        if not fi.empty and "total_shares" in stk.columns and "net_profit" in fi.columns:
            date_ts = pd.Timestamp(to_str(date)) if not isinstance(date, str) else pd.Timestamp(date)
            # P0 (2026-08-29): PIT 披露过滤替代 stat_date<=
            sub = fi[pit_visible_mask(fi, date)].copy()
            if not sub.empty:
                sub["eps"] = sub["net_profit"] / sub["symbol"].map(stk["total_shares"])
                sub = sub[sub["eps"].notna()]
                results = _sue_from_rows(sub, fundamentals.index)
                if results:
                    return _cs_zscore(pd.Series(results, name="sue"), sparse=True).rename("sue")
                return pd.Series(np.nan, index=fundamentals.index, name="sue")

    db = _market_db_path()
    conn = _db_connect()

    _syms = fundamentals.index.tolist()
    _ph = ",".join(["?"] * len(_syms))
    # v477: 原实现 date 直接作 SQL 参数 — dispatch 传 Timestamp → 
    #       sqlite3 "type Timestamp is not supported" → 因子永远 0 行
    date_str = to_str(date) if not isinstance(date, str) else date

    # 读取季度净利润 + 总股本 (P0 2026-08-29: PIT 披露过滤替代 stat_date<=)
    rows = conn.execute(f"""
        SELECT fi.symbol, fi.stat_date, fi.net_profit, s.total_shares
        FROM financial_income fi
        JOIN stocks s ON fi.symbol = s.symbol
        WHERE ({pit_where_sql()})
          AND fi.symbol IN ({_ph})
          AND s.total_shares IS NOT NULL
          AND s.total_shares > 0
          AND fi.net_profit IS NOT NULL
        ORDER BY fi.symbol, fi.stat_date DESC
    """, list(pit_where_params(date_str)) + _syms).fetchall()

    if not rows:
        return pd.Series(np.nan, index=fundamentals.index, name="sue")

    df = pd.DataFrame(rows, columns=['symbol', 'stat_date', 'net_profit', 'total_shares'])
    df['stat_date'] = pd.to_datetime(df['stat_date'])
    df['eps'] = df['net_profit'] / df['total_shares']

    results = _sue_from_rows(df, fundamentals.index)
    if not results:
        return pd.Series(np.nan, index=fundamentals.index, name="sue")

    sue_series = pd.Series(results, name="sue")
    sue_series = sue_series.replace([np.inf, -np.inf], np.nan)
    sue_series = sue_series.clip(-5, 5)
    # 高SUE→高分
    conn.close()
    return _cs_zscore(sue_series, sparse=True).rename("sue")



def _sue_from_rows(df: "pd.DataFrame", symbols) -> dict:
    """按每股计算 SUE: (eps_latest - eps_yoy) / std(eps_8q).

    与 compute_sue 共用 (aux 快路径 + SQL fallback 同语义).
    """
    results = {}
    df_idx = df.set_index("symbol")
    for sym in symbols:
        if sym not in df_idx.index:
            continue
        sym_data = df_idx.loc[[sym]].sort_values('stat_date', ascending=True)
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
    return results




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
    conn = _db_connect()

    _syms = fundamentals.index.tolist()
    _ph = ",".join(["?"] * len(_syms))
    end_date = pd.Timestamp(date)
    date_str = to_str(date) if not isinstance(date, str) else date

    start_date = end_date - pd.DateOffset(days=60)

    rows = conn.execute(f"""
        SELECT symbol, SUM(CASE WHEN direction='out' THEN change_vol ELSE 0 END) as total_out_vol
        FROM holder_trade
        WHERE ann_date BETWEEN ? AND ?
          AND symbol IN ({_ph})
        GROUP BY symbol
    """, [start_date.strftime("%Y-%m-%d"), date_str] + _syms).fetchall()

    vals = {r[0]: r[1] for r in rows if r[1] is not None}
    result = pd.Series(vals, name="holder_reduction")
    result = result.replace([float('inf'), float('-inf')], float('nan'))
    # 高减持→低分 (IC为负)
    # 注: akshare 只返回绝对股数, 横截面 z-score 标准化已处理量纲差异
    conn.close()
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
    conn = _db_connect()

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
    conn.close()
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
    conn = _db_connect()

    _syms = fundamentals.index.tolist()
    _ph = ",".join(["?"] * len(_syms))

    # 取最近12个月分红
    date_str = to_str(date) if not isinstance(date, str) else date
    end_date = pd.Timestamp(date_str)
    start_date = end_date - pd.DateOffset(months=12)

    div_rows = conn.execute(f"""
        SELECT symbol, SUM(cash_div) as total_div
        FROM dividend
        WHERE record_date BETWEEN ? AND ?
          AND symbol IN ({_ph})
          AND cash_div IS NOT NULL
        GROUP BY symbol
    """, [start_date.strftime("%Y-%m-%d"), date_str] + _syms).fetchall()

    # v406: 股息率 = 每股股息 / 股价, 不是股息额
    # 原实现只取了 div 直接做截面标准化, 实际是"股息额因子"而非"股息率"
    # v477: 原实现 SELECT close_latest FROM stocks — 该列不存在 (PRAGMA 实证),
    #       SQL 异常 → 因子永远 0 行; 改为当日最近收盘价 (daily 表)
    price_rows = conn.execute(f"""
        SELECT d.symbol, d.close
        FROM daily d
        JOIN (SELECT symbol, MAX(date) AS mx FROM daily
              WHERE date <= ? AND symbol IN ({_ph}) GROUP BY symbol) t
          ON d.symbol = t.symbol AND d.date = t.mx
    """, [date_str] + _syms).fetchall()
    price_map = {r[0]: r[1] for r in price_rows if r[1] and r[1] > 0}

    div_map = {r[0]: r[1] for r in div_rows if r[1] and r[1] > 0}
    vals = {}
    for sym in fundamentals.index:
        div = div_map.get(sym)
        price = price_map.get(sym)
        if div and div > 0 and price and price > 0:
            vals[sym] = div / price  # 股息率 = 每股股息 / 股价

    result = pd.Series(vals, name="dividend_yield")
    result = result.replace([float('inf'), float('-inf')], float('nan'))
    # 高股息→高分
    conn.close()
    return _cs_zscore(result, sparse=True).rename("dividend_yield")




# ═══════════════════════════════════════════════════════════
# P70: 四新因子 — OIR 昼夜 / STR 量稳 / ABN_TURN 残差 / OCFP 现金流
# 来源: 2021-2026 券商金工研报系统搜索, docs/research/四因子接入分析_2026-07-07.md
# ═══════════════════════════════════════════════════════════


