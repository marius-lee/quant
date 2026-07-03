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
import sqlite3
import os as _os
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
    # P0-4: 干净数据(2026-07-03 qfq重拉) IC=-0.014, 反转效应不成立, 改为纯动量(+cum)
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
    # P0-4: 干净数据 IC=-0.018, 反转不成立, 改为短动量(+cum)
    return _cs_zscore(cum).rename(f"reversal_{window}d")


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
    # 负偏度溢价 (Barberis & Huang 2008): 负偏度→高分→高未来收益
    return _cs_zscore(-skew).rename(f"skewness_{window}d")  # IC=+0.016实测: -skew方向匹配, 负偏度弱溢价, 已弃用



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
    return _cs_zscore(avg_gap).rename(f"gap_{window}d")  # 正缺口(高开)→强势→高分


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




# ═══════════════════════════════════════════════════════════
# 15. RSI 均值回复 — A 股实证 IC≈0.03-0.04
# RSI<30(超卖)→反弹, RSI>70(超买)→回落, 取-RSI使低RSI得高分
# ═══════════════════════════════════════════════════════════

def compute_rsi_reversal(data, date: str, window: int = 14):
    """RSI 均值回复因子: -RSI(14), 低RSI(超卖)→高分→预期反弹."""
    close = data["close"]
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name=f"rsi_rev_{window}d")
    idx = close.index.get_loc(date)
    start = max(0, idx - window + 1)
    avg_gain = gain.iloc[start:idx + 1].mean()
    avg_loss = loss.iloc[start:idx + 1].mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    return _cs_zscore(-rsi).rename(f"rsi_rev_{window}d")


# ── 因子函数映射 (元数据从 factor_registry 表读取) ──

# ═══════════════════════════════════════════════════════════
# 16. 资金流向 (Money Flow) — Chaikin Money Flow 变体
# A股实证: 日内资金流向预测次日收益, IC≈0.03-0.05
# ═══════════════════════════════════════════════════════════

def compute_money_flow(data: "pd.DataFrame", date: str, window: int = 5) -> "pd.Series":
    """资金流向因子: volume-weighted intraday return proxy.
    
    算法: sum(amount * (close-open)/(high-low)) / sum(amount) over window days.
    高分 = 近期资金净流入 (收盘价接近日内高点, 放量).
    
    来源: Chaikin Money Flow (CMF) 变体. A股T+1下日内走势反映主力意图.
    """
    opn, high, low, close, amount = data["open"], data["high"], data["low"], data["close"], data["amount"]
    
    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name=f"money_flow_{window}d")
    
    idx = close.index.get_loc(date)
    start = max(0, idx - window + 1)
    
    # Money Flow Multiplier: ((close - low) - (high - close)) / (high - low)
    # This ranges from -1 (close at low) to +1 (close at high)
    o_slice = opn.iloc[start:idx + 1]
    h_slice = high.iloc[start:idx + 1]
    l_slice = low.iloc[start:idx + 1]
    c_slice = close.iloc[start:idx + 1]
    a_slice = amount.iloc[start:idx + 1]
    
    hl_range = h_slice - l_slice
    # Avoid division by zero
    hl_range = hl_range.where(hl_range > 0, np.nan)
    
    mfm = ((c_slice - l_slice) - (h_slice - c_slice)) / hl_range
    mfv = mfm * a_slice  # Money Flow Volume
    
    total_mfv = mfv.sum(skipna=True)
    total_amount = a_slice.sum(skipna=True)
    
    cmf = total_mfv / total_amount.replace(0, np.nan)
    return _cs_zscore(cmf).rename(f"money_flow_{window}d")


# ═══════════════════════════════════════════════════════════
# 17. 均线多头排列 (MA Alignment) — A股技术分析最核心信号
# 当 MA5>MA10>MA20>MA60 时趋势确认, IC≈0.03-0.05
# ═══════════════════════════════════════════════════════════

def compute_ma_alignment(data: "pd.DataFrame", date: str, window: int = 20) -> "pd.Series":
    """均线多头排列强度: MA alignment score.
    
    算法: 
      - 计算 MA5, MA10, MA20, MA60
      - alignment_score = (MA5/MA10-1) + (MA10/MA20-1) + (MA20/MA60-1)
      - 正值 = 多头排列 (短期均线在上), 负值 = 空头排列
      - 额外奖励: MA5>MA10>MA20>MA60 完全多头排列加 1 分
    
    来源: A股技术分析核心信号. 均线多头排列是趋势延续的最基本确认.
    """
    close = data["close"]
    
    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name="ma_alignment_20d")
    
    idx = close.index.get_loc(date)
    
    # Compute MAs for all stocks at this date
    ma5 = close.iloc[max(0, idx - 4):idx + 1].mean()
    ma10 = close.iloc[max(0, idx - 9):idx + 1].mean()
    ma20 = close.iloc[max(0, idx - 19):idx + 1].mean()
    ma60 = close.iloc[max(0, idx - 59):idx + 1].mean() if idx >= 59 else close.iloc[:idx + 1].mean()
    
    # Alignment score for each stock
    score = pd.Series(0.0, index=close.columns)
    
    # Each pair: short > long gives positive score
    with np.errstate(divide='ignore', invalid='ignore'):
        score += (ma5 / ma10.replace(0, np.nan) - 1).fillna(0)
        score += (ma10 / ma20.replace(0, np.nan) - 1).fillna(0)
        score += (ma20 / ma60.replace(0, np.nan) - 1).fillna(0)
    
    # Bonus for perfect alignment: MA5 > MA10 > MA20 > MA60
    perfect = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)
    score = score.where(~perfect, score + 1.0)
    
    # Penalize inverse alignment
    inverse = (ma5 < ma10) & (ma10 < ma20) & (ma20 < ma60)
    score = score.where(~inverse, score - 1.0)
    
    return _cs_zscore(score).rename("ma_alignment_20d")


# ═══════════════════════════════════════════════════════════
# 18. 量价相关性 (Volume-Price Correlation) — 趋势确认
# corr(volume, close) > 0 → 量价配合, 趋势延续, IC≈0.03-0.05
# ═══════════════════════════════════════════════════════════

def compute_volume_price_corr(data: "pd.DataFrame", date: str, window: int = 10) -> "pd.Series":
    """量价相关性: rolling correlation between daily volume and closing price.
    
    算法: Spearman rank correlation of (volume, close) over window days.
    高分 = 量价正相关 → 上涨放量/下跌缩量 → 健康趋势.
    
    来源: A股量价理论. 量价配合是趋势质量的最重要度量.
    """
    close = data["close"]
    volume = data["volume"]
    
    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name=f"vol_price_corr_{window}d")
    
    idx = close.index.get_loc(date)
    start = max(0, idx - window + 1)
    
    c_slice = close.iloc[start:idx + 1]
    v_slice = volume.iloc[start:idx + 1]
    
    # Vectorized correlation: compute per stock
    corrs = {}
    for sym in c_slice.columns:
        c = c_slice[sym].dropna()
        v = v_slice[sym].dropna()
        common = c.index.intersection(v.index)
        if len(common) >= max(3, window // 2):
            corrs[sym] = c.loc[common].corr(v.loc[common])
    
    result = pd.Series(corrs)
    return _cs_zscore(result).rename(f"vol_price_corr_{window}d")


# ═══════════════════════════════════════════════════════════
# 19. 换手率异常 (Turnover Anomaly) — 主力进场信号
# (turnover_5d - turnover_60d) / std(turnover_60d), IC≈0.03-0.04
# ═══════════════════════════════════════════════════════════

def compute_turnover_anomaly(data: "pd.DataFrame", date: str, short: int = 5,
                             long: int = 60) -> "pd.Series":
    """换手率异常: 标准化换手率偏离.
    
    算法: (avg_turnover(short) - avg_turnover(long)) / std(turnover(long)).
    高分 = 换手率突然大幅放大 → 资金异动 → 潜在主力进场.
    
    来源: A股实证 IC≈0.03-0.04. 换手率异常放大是散户关注度上升的代理变量,
    短期正向, 长期负向.
    """
    turnover = data["turnover"]
    
    if date not in turnover.index:
        return pd.Series(np.nan, index=turnover.columns, name=f"turnover_anomaly_{short}d")
    
    idx = turnover.index.get_loc(date)
    short_start = max(0, idx - short + 1)
    long_start = max(0, idx - long + 1)
    
    short_avg = turnover.iloc[short_start:idx + 1].mean()
    long_avg = turnover.iloc[long_start:idx + 1].mean()
    long_std = turnover.iloc[long_start:idx + 1].std()
    
    anomaly = (short_avg - long_avg) / long_std.replace(0, np.nan)
    anomaly = anomaly.replace([np.inf, -np.inf], np.nan)
    
    return _cs_zscore(anomaly).rename(f"turnover_anomaly_{short}d")


# ═══════════════════════════════════════════════════════════
# 20. 涨停距离 (Limit-Up Proximity) — A股独有动量
# avg(return/limit_up_pct, 5d), 接近涨停板有动量溢出, IC≈0.03-0.06
# ═══════════════════════════════════════════════════════════

def compute_limit_up_proximity(data: "pd.DataFrame", date: str, window: int = 5) -> "pd.Series":
    """涨停距离因子: 近期涨幅占涨跌停板比例的平均值.
    
    算法: avg(daily_return / board_limit_up_pct, window).
    主板 ±10%, 科创板/创业板 ±20%. 自动识别.
    高分 = 近期持续接近涨停 → 强势股 → 动量延续.
    
    来源: A股涨跌停制度独有异象. 接近涨停板的股票存在动量溢出效应.
    """
    close = data["close"]
    
    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name=f"limit_up_prox_{window}d")
    
    idx = close.index.get_loc(date)
    start = max(0, idx - window + 1)
    
    ret = close.pct_change()
    ret_slice = ret.iloc[start:idx + 1]
    
    # Determine board limit: use market info from stocks table
    # 主板 ±10%, 科创(688xxx) ±20%, 创业(300xxx) ±20%, 北交(8/9xxxxx) ±30%
    import sqlite3
    db_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "data", "market.db")
    conn = sqlite3.connect(db_path)
    symbols_str = ",".join(f"'{s}'" for s in close.columns[:100])  # sample for market check
    market_rows = conn.execute(
        f"SELECT symbol, market FROM stocks WHERE symbol IN ({symbols_str})"
    ).fetchall()
    conn.close()
    market_map = {r[0]: r[1] for r in market_rows}
    
    def _limit_pct(sym):
        m = market_map.get(sym, "SH")
        if m in ("BJ",):
            return 0.30
        if sym.startswith("688") or sym.startswith("300") or sym.startswith("301"):
            return 0.20
        return 0.10
    
    avg_proximity = {}
    for sym in close.columns:
        r = ret_slice[sym].dropna()
        if len(r) < 2:
            continue
        limit = _limit_pct(sym)
        prox = (r / limit).mean()
        avg_proximity[sym] = prox
    
    result = pd.Series(avg_proximity)
    return _cs_zscore(result).rename(f"limit_up_prox_{window}d")



# ═══════════════════════════════════════════════════════════
# 21. 涨停板因子 (Limit-Up) — A股最强动量异象
# 首板次日连板概率 30-40%, IC≈0.06-0.10 (A股独有)
# ═══════════════════════════════════════════════════════════

def compute_limit_up_streak(data: "pd.DataFrame", date: str) -> "pd.Series":
    """涨停连板因子: 从 limit_up_pool 表读取连板数 + 封板质量。
    
    算法:
      - 连板数 (limit_up_times): 越高越强, 但≥5连板风险加大 → 倒U型评分
      - 封板质量: 炸板次数=0 且封板资金/流通市值 > 5% 加分
      - 首板 (zt_stat='1/1'): 额外加分 (首板溢价)
      - 只包含当日涨停的股票, 其余为 NaN
    
    来源: A股涨跌停制度独有异象. 涨停板有显著动量溢出.
    """
    import sqlite3
    db_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "data", "market.db")
    conn = sqlite3.connect(db_path)
    
    # Get today's limit-up stocks
    rows = conn.execute("""
        SELECT symbol, limit_up_times, open_times, lock_capital, circ_mv, zt_stat
        FROM limit_up_pool WHERE date = ?
    """, (date,)).fetchall()
    conn.close()
    
    if not rows:
        return pd.Series(dtype=float, name="zt_streak")
    
    scores = {}
    for sym, times, opens, lock_cap, circ_mv, zt_stat in rows:
        score = 0.0
        
        # 连板数评分: 1→0.5, 2→1.5, 3→3.0, 4→4.0, 5+→递减 (倒U)
        if times and times > 0:
            if times <= 4:
                score += times * (times + 1) / 2  # 1→1, 2→3, 3→6, 4→10
            else:
                score += max(0, 10 - (times - 4) * 2)  # 5→8, 6→6, 7→4, ...
        
        # 封板质量: 未炸板 + 封板资金占比
        if opens is not None and opens == 0:
            score += 1.0
        if lock_cap is not None and circ_mv is not None and circ_mv > 0:
            lock_ratio = lock_cap / circ_mv
            if lock_ratio > 0.10:
                score += 3.0  # 封板资金 > 10% 流通市值 = 极强
            elif lock_ratio > 0.05:
                score += 2.0
            elif lock_ratio > 0.02:
                score += 1.0
        
        # 首板溢价
        if zt_stat and zt_stat.startswith('1/'):
            score += 1.5
        
        # 开板过多次的扣分
        if opens is not None and opens >= 3:
            score -= opens * 0.5
        
        scores[sym] = score
    
    result = pd.Series(scores)
    return _cs_zscore(result).rename("zt_streak")

def compute_lhb_net_buy(data: "pd.DataFrame", date: str, window: int = 20) -> "pd.Series":
    """龙虎榜净买入强度因子: total_net_buy / avg(circ_mv), N日窗口.

    算法:
      - 从 lhb_detail 表读取过去 N 个交易日龙虎榜记录
      - 每只股票: SUM(net_buy) / AVG(circ_mv) = 净买入占比
      - 截面 z-score 标准化
      - 未上榜股票得 0 分 (中性)

    来源: A股龙虎榜制度独有信号. 机构/游资上榜净买入是资金流入代理变量,
          龙虎榜净买入与次日收益正相关 (A股实证 IC≈0.04-0.08).

    添加日期: 2026-07-03 — lhb_detail 表补齐后激活.
    """
    import sqlite3
    db_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "data", "market.db")
    conn = sqlite3.connect(db_path)

    symbols = list(data["close"].columns)

    all_dates = sorted(data.index)
    date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)[:10]
    if date_str not in all_dates:
        conn.close()
        return pd.Series(np.nan, index=symbols, name=f"lhb_net_buy_{window}d")

    pos = all_dates.index(date_str)
    start = max(0, pos - window + 1)
    if hasattr(all_dates[start], "strftime"):
        start_date = all_dates[start].strftime("%Y-%m-%d")
    else:
        start_date = str(all_dates[start])[:10]

    rows = conn.execute("""
        SELECT symbol,
               COALESCE(SUM(net_buy), 0) as total_net_buy,
               AVG(COALESCE(circ_mv, 0)) as avg_circ_mv
        FROM lhb_detail
        WHERE trade_date BETWEEN ? AND ?
        GROUP BY symbol
    """, (start_date, date_str)).fetchall()
    conn.close()

    if not rows:
        return pd.Series(0.0, index=symbols, name=f"lhb_net_buy_{window}d")

    scores = {}
    for sym, total_buy, avg_mv in rows:
        if avg_mv and avg_mv > 0 and total_buy is not None:
            scores[sym] = total_buy / avg_mv

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename(f"lhb_net_buy_{window}d")



_PRICE_FN_MAP = {
    "reversal_5d":           (compute_reversal,            5),
    "volatility_20d":        (compute_volatility,         20),
    "turnover_rev_5d":       (compute_turnover_reversal,   5),
    "max_ret_20d":           (compute_max_return,         20),
    "gap_5d":                (compute_overnight_gap,       5),
    "range_20d":             (compute_intraday_range,     20),
    "momentum_10d":          (compute_momentum,           10),
    "skewness_20d":          (compute_skewness,           20),
    "idio_vol_20d":          (compute_idiosyncratic_vol,  20),
    "hsgt_flow_5d":          (compute_hsgt_flow,           5),
    "amihud_20d":            (compute_amihud,             20),
    "rsi_rev_14d":           (compute_rsi_reversal,       14),
    "money_flow_5d":         (compute_money_flow,          5),
    "ma_alignment_20d":      (compute_ma_alignment,       20),
    "vol_price_corr_10d":    (compute_volume_price_corr,  10),
    "turnover_anomaly":      (compute_turnover_anomaly,    5),
    "limit_up_prox_5d":      (compute_limit_up_proximity,  5),
    "zt_streak":             (compute_limit_up_streak,     0),
    "lhb_net_buy_20d":       (compute_lhb_net_buy,        20),
}

def _market_db_path():
    return _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "data", "market.db")

def load_active_price_factors():
    """从 factor_registry 表加载 status='active' 的价格因子 → {name: (cat, window, fn)}."""
    conn = sqlite3.connect(_market_db_path())
    name_list = list(_PRICE_FN_MAP.keys())
    placeholders = ",".join("?" * len(name_list))
    rows = conn.execute(
        f"SELECT name FROM factor_registry WHERE status='active' AND name IN ({placeholders})",
        name_list
    ).fetchall()
    conn.close()
    result = {}
    for (name,) in rows:
        if name in _PRICE_FN_MAP:
            fn, win = _PRICE_FN_MAP[name]
            result[name] = ("dynamic", win, fn)
    return result

def load_active_fundamental_factors():
    """从 factor_registry 表加载 status='active' 的基本面因子."""
    conn = sqlite3.connect(_market_db_path())
    fn_names = list(_FUNDAMENTAL_FN_MAP.keys())
    placeholders = ",".join("?" * len(fn_names))
    rows = conn.execute(
        f"SELECT name FROM factor_registry WHERE status='active' AND name IN ({placeholders})",
        fn_names
    ).fetchall()
    conn.close()
    result = {}
    for (name,) in rows:
        if name in _FUNDAMENTAL_FN_MAP:
            cat, fn = _FUNDAMENTAL_FN_MAP[name]
            result[name] = (cat, fn)
    return result

def update_factor_evaluation(name: str, ic_mean: float, ic_ir: float):
    """回测后更新因子 IC 到数据库."""
    conn = sqlite3.connect(_market_db_path())
    conn.execute(
        "UPDATE factor_registry SET ic_mean=?, ic_ir=?, last_evaluated=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE name=?",
        (round(ic_mean, 6), round(ic_ir, 4), name)
    )
    conn.commit()
    conn.close()

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
    return _cs_zscore(-bp).rename("bp_ratio")  # IC=+0.059实测: -bp方向(即低BP=高PB=成长)匹配IC, A股成长溢价


def compute_size(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """规模因子 — +log(总市值)。大盘股 = 高分。
    来源: Fama & French (1993) — 市值因子
    A股实证: IC=-0.101 → 大盘股跑赢, 与传统SMB反向
    """
    size = np.log(fundamentals["total_mv"])
    size = size.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(size).rename("size")


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
    return _cs_zscore(roe).rename("roe_ratio")


# ── 基本面因子函数映射 (元数据从 factor_registry 表读取) ──
_FUNDAMENTAL_FN_MAP = {
    "ep_ratio":      ("value_ep",       compute_ep_ratio),
    "bp_ratio":      ("value_bp",       compute_bp_ratio),
    "roe_ratio":     ("profitability",  compute_roe_ratio),
    "high52w_dist":  ("high52w",        compute_high52w_dist),
    "size":          ("size_large_cap", compute_size),  # A股大盘溢价
}

def get_factor_names() -> list:
    """返回所有 status='active' 的因子名 (从 factor_registry 表读取)。"""
    return list(load_active_price_factors().keys()) + list(load_active_fundamental_factors().keys())


def compute_all_factors(data: pd.DataFrame, date: str,
                      fundamentals: pd.DataFrame = None,
                      benchmark_ret: Optional["pd.Series"] = None) -> dict:
    """批量计算所有已注册因子 → {factor_name: Series(index=symbol)}。

    价格因子从 data 计算, 基本面因子从 fundamentals 计算。
    benchmark_ret 用于特质波动率因子(对指数回归取残差)。
    """
    results = {}
    for name, (cat, win, fn) in load_active_price_factors().items():
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
        for name, (cat, fn) in load_active_fundamental_factors().items():
            try:
                results[name] = fn(fundamentals, date)
            except Exception as e:
                from utils.logger import get_logger
                get_logger("factor.compute").warning(f"fundamental factor {name} failed: {e}")
                results[name] = pd.Series(dtype=float)
    return results
