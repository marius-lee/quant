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

import numpy as np
import pandas as pd
from typing import Optional


# ═══════════════════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════════════════

def _log_returns(close: pd.DataFrame) -> pd.DataFrame:
    """对数收益率, 停牌日返回 NaN (不由 ffill 掩盖)。"""
    return np.log(close).diff()


def _cs_zscore(series: pd.Series, min_count: int = 30) -> pd.Series:
    """截面 z-score 标准化: (x - cross_sectional_mean) / cross_sectional_std.
    若截面有效值<min_count, 返回全 NaN。"""
    if series.count() < min_count:
        return pd.Series(np.nan, index=series.index)
    return (series - series.mean()) / series.std(ddof=1)


# ═══════════════════════════════════════════════════════════
# 1. 动量因子 — Jegadeesh & Titman (1993)
# ═══════════════════════════════════════════════════════════

def compute_momentum(data: pd.DataFrame, date: str, window: int) -> pd.Series:
    """价格动量: (P_t / P_{t-window}) - 1.
    
    来源: ② Jegadeesh & Titman (1993) — 过去 3-12 个月赢家继续赢。
    使用对数收益率加总: sum(log_returns[-window:]) 更鲁棒。
    """
    close = data["close"]
    log_ret = _log_returns(close)
    
    if date not in log_ret.index:
        return pd.Series(np.nan, index=close.columns, name=f"momentum_{window}d")
    
    idx = log_ret.index.get_loc(date)
    start = max(0, idx - window + 1)
    cum = log_ret.iloc[start:idx + 1].sum()  # 累加对数收益
    return _cs_zscore(cum).rename(f"momentum_{window}d")


# ═══════════════════════════════════════════════════════════
# 2. 反转因子 — Lehmann (1990)
# ═══════════════════════════════════════════════════════════

def compute_reversal(data: pd.DataFrame, date: str, window: int = 5) -> pd.Series:
    """短期反转: -1 × (过去 window 日收益率)。
    
    来源: ② Lehmann (1990) — 周度收益反转; Jegadeesh (1990) — 月度收益反转。
    在 A 股市场, 短期反转通常比动量更强 (retail-dominated turnover)。
    """
    close = data["close"]
    log_ret = _log_returns(close)
    
    if date not in log_ret.index:
        return pd.Series(np.nan, index=close.columns, name=f"reversal_{window}d")
    
    idx = log_ret.index.get_loc(date)
    start = max(0, idx - window + 1)
    cum = log_ret.iloc[start:idx + 1].sum()
    # 反转 = 负动量
    return _cs_zscore(-cum).rename(f"reversal_{window}d")


# ═══════════════════════════════════════════════════════════
# 3. 波动率因子 — Andersen et al. (2001)
# ═══════════════════════════════════════════════════════════

def compute_volatility(data: pd.DataFrame, date: str, window: int = 20) -> pd.Series:
    """已实现波动率: std(log_returns[-window:]) × sqrt(252) 年化。
    
    来源: ② Andersen et al. (2001) — 已实现波动率作为风险度量。
    低波动异象 (low-vol anomaly): 低波动股票未来收益更高。因子值取 -1 × vol。
    """
    close = data["close"]
    log_ret = _log_returns(close)
    
    if date not in log_ret.index:
        return pd.Series(np.nan, index=close.columns, name=f"volatility_{window}d")
    
    idx = log_ret.index.get_loc(date)
    start = max(0, idx - window + 1)
    # 窗口内日收益的 std, 年化
    vol = log_ret.iloc[start:idx + 1].std() * np.sqrt(252)
    # 低波动→高分 (取负号)
    return _cs_zscore(-vol).rename(f"volatility_{window}d")


def compute_downside_volatility(data: pd.DataFrame, date: str, window: int = 20) -> pd.Series:
    """下行波动率: std(负收益) × sqrt(252)。只惩罚亏损波动。
    
    来源: ② Sortino & Price (1994) — 下行风险比总波动更有信息量。
    """
    close = data["close"]
    log_ret = _log_returns(close)
    
    if date not in log_ret.index:
        return pd.Series(np.nan, index=close.columns, name=f"downside_vol_{window}d")
    
    idx = log_ret.index.get_loc(date)
    start = max(0, idx - window + 1)
    window_ret = log_ret.iloc[start:idx + 1]
    # 只取负收益计算标准差
    down = window_ret.where(window_ret < 0, 0)
    down_vol = down.std() * np.sqrt(252)
    return _cs_zscore(-down_vol).rename(f"downside_vol_{window}d")


# ═══════════════════════════════════════════════════════════
# 4. 成交量因子 — Gervais, Kaniel & Mingelegrin (2001)
# ═══════════════════════════════════════════════════════════

def compute_volume_ratio(data: pd.DataFrame, date: str, window: int = 5,
                         long_window: int = 20) -> pd.Series:
    """量比: avg_volume(short_window) / avg_volume(long_window)。
    
    来源: ② Gervais, Kaniel & Mingelegrin (2001) — 高成交量预示未来收益。
    """
    volume = data["volume"]
    
    if date not in volume.index:
        return pd.Series(np.nan, index=volume.columns, name=f"vol_ratio_{window}d")
    
    idx = volume.index.get_loc(date)
    short_start = max(0, idx - window + 1)
    long_start = max(0, idx - long_window + 1)
    
    short_avg = volume.iloc[short_start:idx + 1].mean()
    long_avg = volume.iloc[long_start:idx + 1].mean()
    ratio = short_avg / long_avg.replace(0, np.nan)
    return _cs_zscore(ratio).rename(f"vol_ratio_{window}d")


def compute_turnover_change(data: pd.DataFrame, date: str, window: int = 5) -> pd.Series:
    """换手率变化: (turnover_t - turnover_{t-window}) / turnover_{t-window}。
    
    来源: ② 换手率上升通常伴随短期 alpha, 但长期均值回复。
    """
    turnover = data["turnover"]
    
    if date not in turnover.index:
        return pd.Series(np.nan, index=turnover.columns, name=f"turnover_chg_{window}d")
    
    idx = turnover.index.get_loc(date)
    prev_idx = max(0, idx - window)
    current = turnover.iloc[idx]
    prev = turnover.iloc[prev_idx]
    chg = (current - prev) / prev.replace(0, np.nan)
    return _cs_zscore(chg).rename(f"turnover_chg_{window}d")


# ═══════════════════════════════════════════════════════════
# 5. Amihud 非流动性因子 — Amihud (2002)
# ═══════════════════════════════════════════════════════════

def compute_amihud(data: pd.DataFrame, date: str, window: int = 20) -> pd.Series:
    """Amihud 非流动性: mean(|r_t| / dollar_volume_t) × 10^6。
    
    来源: ② Amihud (2002) — 非流动性溢价: 流动性差的股票预期收益更高。
    amount 在数据库中单位是千元, dollar_volume = amount × 1000。
    
    高分 = 流动性差 = 预期收益高。
    """
    close = data["close"]
    amount = data["amount"]  # 千元
    
    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name=f"amihud_{window}d")
    
    idx = close.index.get_loc(date)
    start = max(0, idx - window + 1)
    
    # Vectorized Amihud: all stocks at once
    p_slice = close.iloc[start:idx + 1]
    a_slice = amount.iloc[start:idx + 1]
    ret = p_slice.pct_change().abs()
    dollar_vol = a_slice * 1000
    illiq = (ret / dollar_vol.replace(0, np.nan)).mean(skipna=True) * 1e6
    # Stocks with <50% valid data → NaN
    min_valid = max(1, int(window * 0.5))
    valid_mask = (p_slice.count() >= min_valid) & (a_slice.count() >= min_valid)
    illiq = illiq.where(valid_mask)
    
    series = illiq
    # 高分=高非流动性=高预期收益
    return _cs_zscore(series).rename(f"amihud_{window}d")


# ═══════════════════════════════════════════════════════════
# 6. 偏度因子 — Barberis & Huang (2008)
# ═══════════════════════════════════════════════════════════

def compute_skewness(data: pd.DataFrame, date: str, window: int = 20) -> pd.Series:
    """收益率偏度: skew(log_returns[-window:])。
    
    来源: ② Barberis & Huang (2008) — 正偏度股票被高估, 负偏度有溢价。
    因子值取负偏度 → 高分=负偏度=高预期收益。
    """
    close = data["close"]
    log_ret = _log_returns(close)
    
    if date not in log_ret.index:
        return pd.Series(np.nan, index=close.columns, name=f"skewness_{window}d")
    
    idx = log_ret.index.get_loc(date)
    start = max(0, idx - window + 1)
    window_ret = log_ret.iloc[start:idx + 1]
    skew = window_ret.skew()
    # A股正偏度溢价: 正偏度→高分→低未来收益(IC<0), 符合Barberis # 负偏度 → 高分 (premium for negative skewness) Huang(2008)
    return _cs_zscore(skew).rename(f"skewness_{window}d")  # 正偏度→高分, IC方向为负



# ═══════════════════════════════════════════════════════════
# 8. 换手率反转 — Lee & Swaminathan (2000), A股实证IC≈0.03-0.05
# ═══════════════════════════════════════════════════════════

def compute_turnover_reversal(data: "pd.DataFrame", date: str, short: int = 5,
                              long: int = 20) -> "pd.Series":
    """换手率反转: -(avg_turnover(short)/avg_turnover(long) - 1).
    高换手→低分(散户追涨效应)。数据字段: daily.turnover(单位:%)。
    若 turnover 覆盖率 <50 stocks，自动退化为成交量反转 (volume 覆盖率 100%)。"""
    to = data["turnover"]
    if date not in to.index:
        return pd.Series(np.nan, index=to.columns, name=f"turnover_rev_{short}d")
    idx = to.index.get_loc(date)
    s = to.iloc[max(0,idx-short+1):idx+1].mean()
    l = to.iloc[max(0,idx-long+1):idx+1].mean()
    result = -(s / l.replace(0, np.nan) - 1)
    # 若有效 turnover 不足 50 只股票, 改用量比 fallback
    if result.dropna().count() < 50:
        vol = data["volume"]
        if date in vol.index:
            vidx = vol.index.get_loc(date)
            vs = vol.iloc[max(0,vidx-short+1):vidx+1].mean()
            vl = vol.iloc[max(0,vidx-long+1):vidx+1].mean()
            result = -(vs / vl.replace(0, np.nan) - 1)
    return _cs_zscore(result).rename(f"turnover_rev_{short}d")


# ═══════════════════════════════════════════════════════════
# 9. 特质波动率 — Ang et al. (2006), 低特质波动异象
# ═══════════════════════════════════════════════════════════

def compute_idiosyncratic_vol(data: "pd.DataFrame", date: str, window: int = 20,
                              benchmark_ret: Optional["pd.Series"] = None) -> "pd.Series":
    """特质波动率: std(残差) 对沪深300回归, 取负号。无bm时退化为总波动率。"""
    close = data["close"]
    log_ret = _log_returns(close)
    if date not in log_ret.index:
        return pd.Series(np.nan, index=close.columns, name=f"idio_vol_{window}d")
    idx = log_ret.index.get_loc(date)
    start = max(0, idx - window + 1)
    wr = log_ret.iloc[start:idx+1]
    if benchmark_ret is not None and not benchmark_ret.empty:
        common = wr.index.intersection(benchmark_ret.index)
        if len(common) >= 10:
            wr, bm = wr.loc[common], benchmark_ret.loc[common]
            bm_c = bm.values - bm.values.mean()
            bm_var = np.dot(bm_c, bm_c)
            if bm_var > 0:
                vols = {}
                for sym in wr.columns:
                    ri = wr[sym].dropna().values
                    if len(ri) < 10: continue
                    ri_c = ri - ri.mean()
                    beta = np.dot(ri_c, bm_c[:len(ri_c)]) / bm_var
                    resid = ri_c - beta * bm_c[:len(ri_c)]
                    vols[sym] = np.std(resid)
                result = pd.Series(vols) * np.sqrt(252)
            else:
                result = wr.std() * np.sqrt(252)
        else:
            result = wr.std() * np.sqrt(252)
    else:
        result = wr.std() * np.sqrt(252)
    return _cs_zscore(-result).rename(f"idio_vol_{window}d")


# ═══════════════════════════════════════════════════════════
# 10. 52周高点距离 — George & Hwang (2004), A股IC≈0.02-0.04
# ═══════════════════════════════════════════════════════════

def compute_high52w_dist(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """接近52周高点→高分。dist = 1 - close_latest/high_52w, 取负号。
    数据字段: stocks.high_52w, stocks.close_latest(当日收盘)"""
    # close_latest 需要从 daily 表补, fundamentals 里只有 stocks 静态字段
    dist = 1.0 - fundamentals["close_latest"] / fundamentals["high_52w"]
    dist = dist.replace([np.inf, -np.inf], np.nan).clip(-2, 2)
    return _cs_zscore(-dist).rename("high52w_dist")

# ═══════════════════════════════════════════════════════════
# 因子注册表

# ═══════════════════════════════════════════════════════════
# 11. 北向资金净流入 — 陆股通 A 股最可靠因子 IC≈0.04-0.06
# ═══════════════════════════════════════════════════════════

def compute_hsgt_flow(data: "pd.DataFrame", date: str, window: int = 5) -> "pd.Series":
    """北向资金净流入因子: avg_net_buy(N日)/circ_mv, z-scored。
    数据源: data/northbound.py → northbound_flow 表 (需先运行 sync_northbound)。
    """
    from data.northbound import get_northbound_flow
    symbols = list(data["close"].columns)
    flow = get_northbound_flow(symbols, date, window=window)
    if flow.empty:
        return pd.Series(np.nan, index=symbols, name=f"hsgt_flow_{window}d")
    return _cs_zscore(flow).rename(f"hsgt_flow_{window}d")


# ═══════════════════════════════════════════════════════════
# 因子注册表 (经 IC/IR 实证验证, 2025Q1-2026Q2 截面评估)
# ═══════════════════════════════════════════════════════════
#  动量:     momentum_10d   IC=+0.017  — Jegadeesh & Titman (1993)
#  低波:     volatility_20d IC=+0.034  — 低波动异象 (最强价格因子)
#  偏度:     skewness_20d   IC=-0.016  — 负偏度溢价 (A股取+skew方向)
#  换手反转: turnover_rev_5d           — Lee & Swaminathan (2000)
#  特质波动: idio_vol_20d              — Ang et al. (2006) 特质波动异象
#  北向资金: hsgt_flow_5d              — 陆股通资金流 (数据待同步)
#  流动性:   amihud_20d                — Amihud (2002) 非流动性溢价
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# 12. 极端日收益 (MAX) — Bali, Cakici & Whitelaw (2011)
# A 股实证 IC≈0.03-0.04, 出现过涨停/大阳线的股票后续跑输(彩票效应)
# ═══════════════════════════════════════════════════════════

def compute_max_return(data: "pd.DataFrame", date: str, window: int = 20) -> "pd.Series":
    """极端日收益因子: max(daily_return[-window:]), 取负号。
    高分 = 没有极端收益的股票 (非彩票型)。"""
    close = data["close"]
    ret = close.pct_change()
    if date not in ret.index:
        return pd.Series(np.nan, index=close.columns, name=f"max_ret_{window}d")
    idx = ret.index.get_loc(date)
    start = max(0, idx - window + 1)
    max_ret = ret.iloc[start:idx + 1].max()
    # 极端正收益 → 后续跑输 → 取负号: 低max_ret = 高分
    return _cs_zscore(-max_ret).rename(f"max_ret_{window}d")


# ═══════════════════════════════════════════════════════════
# 13. 隔夜缺口 — A 股 T+1 独有异象, IC≈0.03-0.04
# 持续低开的股票日内往往回补(恐慌性低开→盘中反弹)
# ═══════════════════════════════════════════════════════════

def compute_overnight_gap(data: "pd.DataFrame", date: str, window: int = 5) -> "pd.Series":
    """隔夜缺口因子: avg((open-prev_close)/prev_close, 5日), 取负号。
    高分 = 持续低开的股票 (负缺口→即将回补)。"""
    opn = data["open"]
    close = data["close"]
    # 计算隔夜缺口: (open_t - close_{t-1}) / close_{t-1}
    gap = (opn - close.shift(1)) / close.shift(1)
    if date not in gap.index:
        return pd.Series(np.nan, index=close.columns, name=f"gap_{window}d")
    idx = gap.index.get_loc(date)
    start = max(0, idx - window + 1)
    avg_gap = gap.iloc[start:idx + 1].mean()
    # 负缺口(低开)→回补→高分: 取-gap使负缺口得高分
    return _cs_zscore(-avg_gap).rename(f"gap_{window}d")


# ═══════════════════════════════════════════════════════════
# 14. 日内振幅 — 波动质量, IC≈0.02-0.03
# 窄幅整理→潜在突破, 宽幅震荡→方向不明
# ═══════════════════════════════════════════════════════════

def compute_intraday_range(data: "pd.DataFrame", date: str, window: int = 20) -> "pd.Series":
    """日内振幅因子: avg((high-low)/close, 20日), 取负号。
    高分 = 窄幅整理 (低振幅→蓄势待发)。"""
    h, l, c = data["high"], data["low"], data["close"]
    rng = (h - l) / c
    if date not in rng.index:
        return pd.Series(np.nan, index=c.columns, name=f"range_{window}d")
    idx = rng.index.get_loc(date)
    start = max(0, idx - window + 1)
    avg_range = rng.iloc[start:idx + 1].mean()
    # 窄幅→高分: 取负号
    return _cs_zscore(-avg_range).rename(f"range_{window}d")



FACTOR_REGISTRY = {
    # ── 6 动态因子: A 股实证最强 (全日频, 无基本面依赖) ──
    "reversal_5d":      ("reversal",      5,  compute_reversal),
    "volatility_20d":   ("volatility",   20, compute_volatility),
    "turnover_rev_5d":  ("turnover_rev",  5,  compute_turnover_reversal),
    "max_ret_20d":      ("max_ret",      20, compute_max_return),
    "gap_5d":           ("overnight_gap", 5,  compute_overnight_gap),
    "range_20d":        ("intraday_range",20, compute_intraday_range),
    # ── 辅助/待启用 ──
    "momentum_10d":     ("momentum",     10, compute_momentum),
    "skewness_20d":     ("skewness",     20, compute_skewness),
    "idio_vol_20d":     ("idio_vol",     20, compute_idiosyncratic_vol),
    "hsgt_flow_5d":     ("northbound",   5,  compute_hsgt_flow),
    "amihud_20d":       ("liquidity",    20, compute_amihud),
}

# ═══════════════════════════════════════════════════════════
# 7. 基本面因子 — Fama & French (1992, 1993, 2015)
# ═══════════════════════════════════════════════════════════

def compute_ep_ratio(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """EP 比率 (1/PE) — 价值因子。低PE = 高EP = 高分。
    来源: Fama & French (1992) — 价值因子 (HML)
    """
    ep = 1.0 / fundamentals["pe"]
    ep = ep.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(ep).rename("ep_ratio")


def compute_bp_ratio(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """BP 比率 (1/PB) — 价值因子。低PB = 高BP = 高分。
    过滤 PE<=0 或 PE>1000 的极端值 (PE失真时bp_ratio无意义)。
    来源: Fama & French (1992) — 账面市值比
    """
    bp = 1.0 / fundamentals["pb"]
    bp = bp.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(-bp).rename("bp_ratio")  # A股成长溢价: 低BP(高PB=成长股)→高分, IC正向


def compute_size(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """规模因子 — -log(总市值)。小市值 = 高分。
    来源: Fama & French (1993) — 市值因子 (SMB), A股小盘溢价
    """
    size = -np.log(fundamentals["total_mv"])
    size = size.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(size).rename("size")


def compute_roe_ratio(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """ROE 盈利能力因子 — 盈利能力溢价。高分 = 高ROE = 高预期收益。

    来源: Fama & French (2015) — 盈利能力因子 (RMW)
    使用 stocks.roe 列 (EPS / BVPS 推导)，过滤 ROE>100 极端值。
    """
    roe = fundamentals["roe"].astype(float)
    # 过滤极端 ROE: 负值 或 >100 视为数据错误
    roe = roe.where((roe > 0) & (roe < 100))
    roe = roe.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(roe).rename("roe_ratio")


FUNDAMENTAL_FACTOR_REGISTRY = {
    # ── 静态因子: 季度更新, 作为辅助/行业中性化用 ──
    # 不参与 alpha 合成 (IC 不稳定, 受财报滞后影响)
    "ep_ratio":      ("value_ep",       compute_ep_ratio),
    "bp_ratio":      ("value_bp",       compute_bp_ratio),
    "roe_ratio":     ("profitability",  compute_roe_ratio),
    "high52w_dist":  ("high52w",        compute_high52w_dist),
}

def get_factor_names() -> list:
    """返回所有已注册因子名 (价格 + 基本面)。"""
    return list(FACTOR_REGISTRY.keys()) + list(FUNDAMENTAL_FACTOR_REGISTRY.keys())


def compute_all_factors(data: pd.DataFrame, date: str,
                      fundamentals: pd.DataFrame = None,
                      benchmark_ret: Optional["pd.Series"] = None) -> dict:
    """批量计算所有已注册因子 → {factor_name: Series(index=symbol)}。
    
    价格因子从 data 计算, 基本面因子从 fundamentals 计算。
    benchmark_ret 用于特质波动率因子(对指数回归取残差)。
    """
    results = {}
    for name, (cat, win, fn) in FACTOR_REGISTRY.items():
        try:
            if 'idio_vol' in name and benchmark_ret is not None:
                results[name] = fn(data, date, win, benchmark_ret=benchmark_ret)
            else:
                results[name] = fn(data, date, win)
        except Exception as e:
            from utils.logger import get_logger
            get_logger("factor.compute").warning(f"price factor {name} failed: {e}")
            results[name] = pd.Series(dtype=float)
    if fundamentals is not None and not fundamentals.empty:
        for name, (cat, fn) in FUNDAMENTAL_FACTOR_REGISTRY.items():
            try:
                results[name] = fn(fundamentals, date)
            except Exception as e:
                from utils.logger import get_logger
                get_logger("factor.compute").warning(f"fundamental factor {name} failed: {e}")
                results[name] = pd.Series(dtype=float)
    return results
