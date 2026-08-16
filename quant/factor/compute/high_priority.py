"""高优先级缺失因子 — Fama-French 五因子 + 学术界高 IC 因子.

基于 market.db 现有数据表实现, 零额外数据依赖.
"""
import numpy as np
import pandas as pd
from quant.utils.date import to_str
from quant.factor.registry import _cs_zscore
from quant.utils.logger import get_logger

_log = get_logger(__name__)


def compute_seasonality_12m_1m(data, date, window=None):
    """季节效应: 12个月前同月收益, 跳过最近1月 (Heston & Sadka 2008).

    来源: JFE 2008; A股验证: 招商证券 2019.
    算法: 取 t-12-m 到 t-12 的收益 (跳过最近1月避免短期反转干扰).
    IC≈2-3%, 正向因子.
    数据: daily.close.
    """
    close = data["close"]
    ts = pd.Timestamp(to_str(date))
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
    ts = pd.Timestamp(to_str(date))
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
    ts = pd.Timestamp(to_str(date))
    if ts not in close.index:
        return pd.Series(np.nan, index=close.columns, name="industry_momentum")
    
    idx = close.index.get_loc(ts)
    start = max(0, idx - window + 1)
    if idx - start < 20:
        return pd.Series(np.nan, index=close.columns, name="industry_momentum")
    
    past = close.iloc[start:idx+1]
    ret = past.pct_change().dropna(how='all').mean()

    # v373: groupby 向量化替代双 per-symbol Python 循环
    ind_series = stocks_df["industry"].reindex(ret.index)
    ind_series = ind_series[ind_series.notna() & (ind_series != "") & (ind_series != "nan")]
    if ind_series.empty:
        return pd.Series(np.nan, index=ret.index, name="industry_momentum")

    ind_mom = ret.groupby(ind_series).mean()
    # 至少 2 只股票 per 行业
    valid_inds = ind_series.value_counts()
    ind_mom = ind_mom[valid_inds >= 2]

    result = ind_series.map(ind_mom)
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
    # 索引化一次替代 5208 次全表扫描的 object 比较 (O(N²)→O(N), 物化日耗 95s→<1s)
    cf_idx = cf.set_index("symbol") if not cf.empty else cf
    bs_idx = bs.set_index("symbol") if not bs.empty else bs
    result = {}
    for sym in symbols:
        if sym not in cf_idx.index or sym not in bs_idx.index:
            continue
        cf_sym = cf_idx.loc[[sym]]
        bs_sym = bs_idx.loc[[sym]]
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
