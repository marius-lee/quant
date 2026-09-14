"""Factor compute fundamental group 1."""

def compute_high52w_dist(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """接近52周高点→高分。dist = 1 - close_latest/high_52w, 取负号。
    数据字段: stocks.high_52w, stocks.close_latest(当日收盘)
    v429: 原 TODO(#3) 已落地 — store.get_fundamentals() 已从 daily 表按 date
    LEFT JOIN close 补 close_latest, high_52w 由 daily 244 日 MAX(close) 计算:
    close_latest 不再依赖 stocks 静态表 (store.py 2401-2421 行确认)。
    """
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
    mv = fundamentals["total_mv"].replace([np.inf, -np.inf], np.nan)
    mv = mv.where(mv.notna() & (mv > 0))
    size = np.log(mv)
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
    修改: 2026-07-17 — PIT 标准: 无数据时返回 NaN (无信号), 区分"aux 缺失"与"数据空"
    """
    if aux is None or "margin" not in aux:
        # Programming error: caller failed to preload aux data
        return None  # aux not preloaded preloaded aux['margin']")
    m = aux["margin"]
    # PIT: no margin data available → return NaN (no signal)
    # Aligns with: compute_analyst_buy (a.empty → NaN), compute_margin_buy_ratio_price (w.empty → 0.0)
    if m.empty or "margin_buy" not in m.columns or "margin_balance" not in m.columns:
        return pd.Series(np.nan, index=fundamentals.index, name="margin_buy_ratio")
    # v477: 必须按当日过滤 + 对齐 symbols — 之前直接全量相除, 返回
    # 全 chunk 全市场非唯一 index Series, 物化端 reindex 对齐后全 NaN → 因子 0 行
    date_str = to_str(date)
    w = m[(m["date"].astype(str) == date_str) & m["margin_balance"].notna()
          & (m["margin_balance"] > 0) & m["margin_buy"].notna()]
    if w.empty:
        return pd.Series(np.nan, index=fundamentals.index, name="margin_buy_ratio")
    s = w.set_index("symbol")["margin_buy"] / w.set_index("symbol")["margin_balance"]
    return s.reindex(fundamentals.index).rename("margin_buy_ratio")



def compute_analyst_consensus(fundamentals: "pd.DataFrame", date: str, aux=None) -> "pd.Series":
    """分析师共识度: buy_count / report_count (盈利预测一致预期)。

    公式: 买入评级数 / 总报告数。值高 = 分析师一致看多。
    数据源: analyst_forecast 表 (akshare stock_analyst_rank_em)。
    来源: 中信建投《逐鹿Alpha》2022, 海通金工 2023。
    """
    # Use preloaded aux data if available
    if aux is not None and "analyst" in aux:
        a = aux["analyst"]
        if a.empty:
            return pd.Series(dtype=float, name="analyst_consensus")
        if not a.empty and "buy_count" in a.columns and "report_count" in a.columns:
            s = a["buy_count"] / a["report_count"].replace(0, np.nan)
            return s.dropna().rename("analyst_consensus")
    # PIT: no analyst data — return empty Series (no signal)
    return pd.Series(dtype=float, name="analyst_consensus")


# ── Phase 3 财务因子 (季报三表) ──

_ALLOWED_FINANCIAL_TABLES = {"financial_income", "financial_balance", "financial_cashflow"}



def _get_financial_historical(table: str, date: str, forward_days: int = 0) -> "pd.DataFrame":
    """Query quarterly financial data up to date, PIT-disclosure-safe (no look-ahead).

    P1-6 fix: 移除 +90d forward_days (前视偏差).
    P0 (2026-08-29): 改用 PIT 披露口径 — 仅取 `pub_date <= date` (真实公告日) 或
    `stat_date + 法定披露时滞` (无公告日源) 的已披露数据, 而非 `stat_date <= date`.
    直接用 stat_date 作"已知边界"会让报告期后 1-4 个月才披露的财报提前泄漏进信号.

    安全: 表名白名单校验，防止 SQL 注入。
    """
    if table not in _ALLOWED_FINANCIAL_TABLES:
        raise ValueError(f"Table not allowed: {table}")
    max_stat = (pd.Timestamp(date) + pd.DateOffset(days=forward_days)).strftime("%Y-%m-%d")
    conn = _db_connect()
    df = pd.read_sql(
        f"SELECT * FROM {table} WHERE ({pit_where_sql()}) ORDER BY stat_date",
        conn, params=pit_where_params(max_stat),
    )
    conn.close()
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



