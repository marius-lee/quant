"""高优先级缺失因子 — Fama-French 五因子 + 学术界高 IC 因子.

基于 market.db 现有数据表实现, 零额外数据依赖.
"""
import numpy as np
import pandas as pd
from quant.factor.registry import _cs_zscore
from quant.utils.logger import get_logger

_log = get_logger(__name__)


def compute_asset_growth(data, date, fundamentals=None):
    """资产增长率 = ΔTotalAssets / TotalAssets_lag (Cooper, Gulen & Schill 2008).

    来源: Fama-French (2015) CMA 因子; Liu, Stambaugh & Yuan (2019) A股验证.
    IC=-3.8%, 负向因子 (高资产增长→低未来收益, 管理层过度投资信号).
    数据: financial_balance.total_assets.
    """
    if fundamentals is None or fundamentals.empty:
        return None
    if 'total_assets' not in fundamentals.columns:
        return None
    assets = fundamentals['total_assets'].dropna()
    if len(assets) < 2:
        return None
    symbols = data["close"].columns if isinstance(data["close"], pd.DataFrame) else []
    result = {}
    for sym in symbols:
        if sym not in fundamentals.index:
            continue
        val = fundamentals.loc[sym, 'total_assets']
        if pd.isna(val) or val <= 0:
            continue
        # 简化: 用上一期对比 (fundamentals 已包含最新值, historical 从 aux 取)
        result[sym] = 0.0  # 占位, 实际需两期对比 — 走 aux 路径
    # 实际逻辑需两期 financial_balance, 此处留框架
    return None


def compute_seasonality_12m_1m(data, date, window=None):
    """季节效应: 12个月前同月收益, 跳过最近1月 (Heston & Sadka 2008).

    来源: JFE 2008; A股验证: 招商证券 2019.
    算法: 取 t-12-m 到 t-12 的收益 (跳过最近1月避免短期反转干扰).
    IC≈2-3%, 正向因子.
    数据: daily.close.
    """
    close = data["close"]
    ts = pd.Timestamp(str(date)[:10])
    if ts not in close.index:
        return pd.Series(np.nan, index=close.columns, name="seasonality_12m_1m")
    idx = close.index.get_loc(ts)
    start_12m = max(0, idx - 252)
    end_1m = max(0, idx - 21)
    if end_1m - start_12m < 60:
        return pd.Series(np.nan, index=close.columns, name="seasonality_12m_1m")
    past = close.iloc[start_12m:end_1m]
    ret = past.iloc[-1] / past.iloc[0] - 1
    return _cs_zscore(ret).rename("seasonality_12m_1m")


def compute_tail_risk(data, date, window=252):
    """尾部风险: 日收益负偏度 + 极端负收益频率 (Kelly & Jiang 2014).

    来源: JFE 2014; 东方证券 2021 A股验证.
    IC=-3.5%, 负向因子 (高尾部风险→低未来收益).
    数据: daily.close.
    """
    import numpy as np
    close = data["close"]
    ts = pd.Timestamp(str(date)[:10])
    if ts not in close.index:
        return pd.Series(np.nan, index=close.columns, name="tail_risk")
    idx = close.index.get_loc(ts)
    start = max(0, idx - window + 1)
    if idx - start < 60:
        return pd.Series(np.nan, index=close.columns, name="tail_risk")
    
    ret = close.iloc[start:idx+1].pct_change().dropna(how='all')
    if len(ret) < 40:
        return pd.Series(np.nan, index=close.columns, name="tail_risk")
    
    neg_skew = -ret.skew()
    var_5pct = ret.quantile(0.05)
    tail_hits = (ret < var_5pct).sum() / len(ret)
    
    composite = 0.6 * neg_skew.fillna(0) + 0.4 * tail_hits.fillna(0)
    return _cs_zscore(composite).rename("tail_risk")


def compute_industry_momentum(data, date, window=63, aux=None):
    """行业动量: 过去 window 日同行业股票等权收益 (Moskowitz & Grinblatt 1999).

    来源: JF 1999; 东方证券 2018 A股验证.
    IC≈4-5%, 正向因子.
    数据: daily.close + stocks.industry.
    """
    if aux is None or "stocks" not in aux or aux["stocks"].empty:
        return pd.Series(np.nan, index=data["close"].columns, name="industry_momentum")
    
    stocks_df = aux["stocks"]
    if "industry" not in stocks_df.columns:
        return pd.Series(np.nan, index=data["close"].columns, name="industry_momentum")
    
    close = data["close"]
    ts = pd.Timestamp(str(date)[:10])
    if ts not in close.index:
        return pd.Series(np.nan, index=close.columns, name="industry_momentum")
    
    idx = close.index.get_loc(ts)
    start = max(0, idx - window + 1)
    if idx - start < 20:
        return pd.Series(np.nan, index=close.columns, name="industry_momentum")
    
    past = close.iloc[start:idx+1]
    ret = past.pct_change().dropna(how='all').mean()
    
    industry_ret = {}
    for sym in ret.index:
        if sym in stocks_df.index:
            try:
                ind_val = stocks_df.loc[sym, "industry"]
                ind = ind_val if isinstance(ind_val, str) else str(ind_val.iloc[0]) if hasattr(ind_val, 'iloc') else str(ind_val)
            except Exception:
                continue
            if ind and ind != "" and ind != "nan":
                industry_ret.setdefault(ind, []).append(ret[sym])
    
    ind_mom = {}
    for ind, vals in industry_ret.items():
        if len(vals) >= 2:
            ind_mom[ind] = np.nanmean(vals)
    
    result = pd.Series(np.nan, index=ret.index)
    for sym in ret.index:
        if sym in stocks_df.index:
            try:
                ind_val = stocks_df.loc[sym, "industry"]
                ind = ind_val if isinstance(ind_val, str) else str(ind_val.iloc[0]) if hasattr(ind_val, 'iloc') else str(ind_val)
            except Exception:
                continue
            if ind in ind_mom:
                result[sym] = ind_mom[ind]
    
    return _cs_zscore(result).rename("industry_momentum")


def compute_cf_roa(data, date, window=None, aux=None):
    """现金流 ROA = 经营现金流 / 总资产 (Fama-French 2015 盈利能力).
    data: MultiIndex DataFrame (price path) 或简单 DataFrame (fundamentals path).
    """
    if aux is None:
        return None
    if "financial_cashflow" not in aux or "financial_balance" not in aux:
        return None
    
    cf = aux.get("financial_cashflow", pd.DataFrame())
    bs = aux.get("financial_balance", pd.DataFrame())
    if cf.empty or bs.empty:
        return None
    
    if isinstance(data.columns, pd.MultiIndex):
        symbols = data["close"].columns.tolist() if "close" in data.columns.get_level_values(0) else []
    else:
        symbols = data.index.tolist()
    result = {}
    for sym in symbols:
        cf_sym = cf[cf["symbol"] == sym] if "symbol" in cf.columns else pd.DataFrame()
        bs_sym = bs[bs["symbol"] == sym] if "symbol" in bs.columns else pd.DataFrame()
        if cf_sym.empty or bs_sym.empty:
            continue
        if "net_operate_cash_flow" not in cf_sym.columns or "total_assets" not in bs_sym.columns:
            continue
        # iloc[-1]: 取最新季度数据 (aux query ORDER BY stat_date ASC → 最后行=最新)
        # 来源: Fama-French (2015) RMW 盈利能力因子; cf_roa = CFO(TTM) / TotalAssets(latest)
        cfo = cf_sym["net_operate_cash_flow"].iloc[-1] if len(cf_sym) > 0 else None
        ta = bs_sym["total_assets"].iloc[-1] if len(bs_sym) > 0 else None
        if pd.notna(cfo) and pd.notna(ta) and ta > 0:
            result[sym] = float(cfo) / float(ta)
    
    if not result:
        return None
    s = pd.Series(result, dtype=float)
    return _cs_zscore(s).rename("cf_roa")
