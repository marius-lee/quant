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

# ── 从拆分模块导入共享组件 ──
from config.constants import *
from factor.registry import _cs_zscore, _db_connect, _FIN_FACTORS, _shared_limit_conn

def _log_returns(close: pd.DataFrame) -> pd.DataFrame:
    """对数收益率, 停牌日返回 NaN (不由 ffill 掩盖)。"""
    return np.log(close).diff()


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


def compute_residual_momentum(data: pd.DataFrame, date: str, window: int = 126) -> pd.Series:
    """残差动量: 股票收益扣除基准收益（β=1近似）— Ch.3.7.

    算法: 股票window日对数收益 - 沪深300(000300)同期对数收益 → 截面z-score。
    β=1 近似 (全量β回归需36月滚动窗口, 初始实现先简化)。
    来源: Kakushadze & Serur (2018) Ch.3.7 Eqs 15-17; Blitz et al. (2011).
    """
    close = data["close"]
    log_ret = _log_returns(close)

    if date not in log_ret.index:
        return pd.Series(np.nan, index=close.columns, name="residual_momentum_126d")

    idx = log_ret.index.get_loc(date)
    start = max(0, idx - window + 1)
    cum = log_ret.iloc[start:idx + 1].sum()

    # 基准收益: 沪深300 或 截面均值 fallback
    if "000300" in cum.index:
        bench_ret = cum["000300"]
    else:
        bench_ret = cum.mean()

    residual = cum - bench_ret
    result = _cs_zscore(residual).rename("residual_momentum_126d")
    if "000300" in result.index:
        result = result.drop("000300")
    return result


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


# ═══════════════════════════════════════════════════════════
# 因子窗口参数 — 由 config.yaml factor.windows 驱动, 代码常量为 fallback
# 所有窗口均有文献或业界依据, 详见 config/config.yaml 注释
# ═══════════════════════════════════════════════════════════
from utils.logger import get_logger as _get_logger
from data.store import market_conn as _market_conn

_log = _get_logger("factor.compute")

def compute_volatility(data: pd.DataFrame, date: str, window: int = _VOLATILITY_WINDOW) -> pd.Series:
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


def compute_downside_volatility(data: pd.DataFrame, date: str, window: int = _DOWNSIDE_VOL_WINDOW) -> pd.Series:
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
                         long_window: int = _VOL_RATIO_LONG) -> pd.Series:
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

def compute_amihud(data: pd.DataFrame, date: str, window: int = _AMIHUD_WINDOW) -> pd.Series:
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
    # Stocks with insufficient data → NaN.
    # Use min(window, actual_data_len) so the threshold adapts when the
    # evaluation lookback is shorter than the factor's ideal window.
    effective = min(window, p_slice.shape[0])
    min_valid = max(30, int(effective * 0.5))
    valid_mask = (p_slice.count() >= min_valid) & (a_slice.count() >= min_valid)
    illiq = illiq.where(valid_mask)

    series = illiq
    # 高分=高非流动性=高预期收益
    return _cs_zscore(series).rename(f"amihud_{window}d")


# ═══════════════════════════════════════════════════════════
# 6. 偏度因子 — Barberis & Huang (2008)
# ═══════════════════════════════════════════════════════════

def compute_skewness(data: pd.DataFrame, date: str, window: int = _SKEWNESS_WINDOW) -> pd.Series:
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

def compute_idiosyncratic_vol(data: "pd.DataFrame", date: str, window: int = _IDIO_VOL_WINDOW,
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
#  动量:     momentum_63d/126d/252d   — Jegadeesh & Titman (1993) 标准窗口
#  低波:     volatility_126d            — 低波动异象 (Kakushadze & Serur Ch.3.4)
#  偏度:     skewness_60d               — 负偏度溢价 (Barberis & Huang 2008)
#  换手反转: turnover_rev_5d            — Lee & Swaminathan (2000)
#  特质波动: idio_vol_126d              — Ang et al. (2006)
#  流动性:   amihud_250d                — Amihud (2002) 非流动性溢价
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# 12. 极端日收益 (MAX) — Bali, Cakici & Whitelaw (2011)
# A 股实证 IC≈0.03-0.04, 出现过涨停/大阳线的股票后续跑输(彩票效应)
# ═══════════════════════════════════════════════════════════

def compute_max_return(data: "pd.DataFrame", date: str, window: int = _MAX_RET_WINDOW) -> "pd.Series":
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

def compute_intraday_range(data: "pd.DataFrame", date: str, window: int = _RANGE_WINDOW) -> "pd.Series":
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
    
    均线窗口固定为 MA5/MA10/MA20/MA60 (不可调).
    alignment_score = (MA5/MA10-1) + (MA10/MA20-1) + (MA20/MA60-1).
    正值 = 多头排列, 负值 = 空头排列. 完全多头排列加 1 分.
    
    来源: A股技术分析核心信号.
    """
    close = data["close"]
    
    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name="ma_alignment")
    
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
        if len(common) >= max(3, window // 2) and c.loc[common].std() > 0 and v.loc[common].std() > 0:
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
    db_path = _market_db_path()
    conn = _db_connect()
    symbols_sample = list(close.columns[:100])  # sample for market check
    placeholders = ",".join("?" * len(symbols_sample))
    market_rows = conn.execute(
        f"SELECT symbol, market FROM stocks WHERE symbol IN ({placeholders})",
        symbols_sample
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

def compute_limit_up_streak(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """涨停连板因子: 从 data OHLCV 自算涨停 + 连板数(不依赖 limit_up_pool)。

    算法:
      - 主板(60/00开头): 涨停 = 日收益 >= 9.5% 且 close == high
      - 科创/创业(68/30开头): 涨停 = 日收益 >= 19.5% 且 close == high
      - 连板数 = 从今日往前连续涨停的天数
      - 倒U型评分: 1连板→1, 2→3, 3→6, 4→10, 5→8, 6+→递减

    来源: A股涨跌停制度独有异象. 涨停板有显著动量溢出.
    修改: 2026-07-03 — 从 limit_up_pool 改为 data OHLCV 自算, 覆盖 6 年历史.
          limit_up_pool 仍每日积累(封板资金/炸板次数), 但不用于因子计算.
    """
    close = data["close"]
    high = data["high"]

    # 匹配日期索引 (兼容 Timestamp 和 string)
    date_str = str(date)[:10]
    matched = [d for d in close.index if str(d)[:10] == date_str]
    if not matched:
        return pd.Series(dtype=float, name="zt_streak")

    idx = close.index.get_loc(matched[0])
    symbols = list(close.columns)

    # 往前看 5 个交易日判断连板 (最多 5 连板, 超过递减)
    lookback = 5
    start = max(0, idx - lookback)
    cw = close.iloc[start:idx + 1]
    hw = high.iloc[start:idx + 1]

    ret = cw.pct_change()  # row 0 = NaN (无前一日 close)

    # 涨停幅度: 科创/创业板 20%, 主板 10%
    limit_map = {}
    for sym in symbols:
        if sym.startswith('30') or sym.startswith('68'):
            limit_map[sym] = 19.5
        else:
            limit_map[sym] = 9.5

    # 今日是否涨停
    today_ret = ret.iloc[-1] * 100
    today_close = cw.iloc[-1]
    today_high = hw.iloc[-1]

    scores = {}
    for sym in symbols:
        lim = limit_map[sym]
        r = today_ret.get(sym)
        c = today_close.get(sym)
        h = today_high.get(sym)
        if pd.isna(r) or pd.isna(c) or pd.isna(h):
            continue
        if not (r >= lim and c == h and h > 0):
            continue  # 今日未涨停 → 无信号

        # 往前数连板
        streak = 1
        for j in range(len(cw) - 2, -1, -1):
            rj = ret.iloc[j].get(sym)
            cj = cw.iloc[j].get(sym)
            hj = hw.iloc[j].get(sym)
            if pd.isna(rj) or pd.isna(cj) or pd.isna(hj):
                break
            if (rj * 100 >= lim) and (cj == hj and hj > 0):
                streak += 1
            else:
                break

        # 倒U型评分: 连板越多越强, 但 >=5 连板风险加大
        if streak <= 4:
            scores[sym] = streak * (streak + 1) / 2  # 1→1, 2→3, 3→6, 4→10
        else:
            scores[sym] = max(0, 10 - (streak - 4) * 2)  # 5→8, 6→6, 7→4, ...

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("zt_streak")


def compute_dt_streak(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """跌停连板因子: zt_streak 的镜像 — 从 data OHLCV 自算跌停 + 连板数。

    算法:
      - 主板(60/00开头): 跌停 = 日收益 <= -9.5% 且 close == low
      - 科创/创业(68/30开头): 跌停 = 日收益 <= -19.5% 且 close == low
      - 连板数 = 从今日往前连续跌停的天数
      - 负向评分(镜像倒U): 1连板→-1, 2→-3, 3→-6, 4→-10, 5→-8, 6+→递减
      - 跌停后大概率继续下跌(A股实证~70%), 连板越多负信号越强

    来源: A股涨跌停制度独有异象. 跌停板有显著的负向动量溢出.
    添加: 2026-07-03 — zt_streak 镜像, Phase 7 P1.
    """
    close = data["close"]
    low = data["low"]

    # 匹配日期索引
    date_str = str(date)[:10]
    matched = [d for d in close.index if str(d)[:10] == date_str]
    if not matched:
        return pd.Series(dtype=float, name="dt_streak")

    idx = close.index.get_loc(matched[0])
    symbols = list(close.columns)

    # 往前看 5 个交易日判断连板
    lookback = 5
    start = max(0, idx - lookback)
    cw = close.iloc[start:idx + 1]
    lw = low.iloc[start:idx + 1]

    ret = cw.pct_change()

    # 跌停幅度: 科创/创业板 -20%, 主板 -10%
    limit_map = {}
    for sym in symbols:
        if sym.startswith('30') or sym.startswith('68'):
            limit_map[sym] = -19.5
        else:
            limit_map[sym] = -9.5

    today_ret = ret.iloc[-1] * 100
    today_close = cw.iloc[-1]
    today_low = lw.iloc[-1]

    scores = {}
    for sym in symbols:
        lim = limit_map[sym]
        r = today_ret.get(sym)
        c = today_close.get(sym)
        lo = today_low.get(sym)
        if pd.isna(r) or pd.isna(c) or pd.isna(lo):
            continue
        if not (r <= lim and c == lo and lo > 0):
            continue  # 今日未跌停 → 无信号

        # 往前数连板
        streak = 1
        for j in range(len(cw) - 2, -1, -1):
            rj = ret.iloc[j].get(sym)
            cj = cw.iloc[j].get(sym)
            lj = lw.iloc[j].get(sym)
            if pd.isna(rj) or pd.isna(cj) or pd.isna(lj):
                break
            if (rj * 100 <= lim) and (cj == lj and lj > 0):
                streak += 1
            else:
                break

        # 负向评分(镜像倒U): 连板越多负得越强
        if streak <= 4:
            scores[sym] = -streak * (streak + 1) / 2  # 1→-1, 2→-3, 3→-6, 4→-10
        else:
            scores[sym] = -(max(0, 10 - (streak - 4) * 2))  # 5→-8, 6→-6, 7→-4, ...

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("dt_streak")


def compute_lhb_net_buy(data: "pd.DataFrame", date: str, window: int = _LHB_WINDOW) -> "pd.Series":
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
    db_path = _market_db_path()
    conn = _db_connect()

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



def compute_lhb_post_quality(data: "pd.DataFrame", date: str, window: int = 90) -> "pd.Series":
    """LHB 上榜后质量因子: 历史上榜后 post_5d 平均收益。

    算法:
      - 从 lhb_detail 读取过去 window 天上榜记录 (排除最近5天, post_5d未实现)
      - 每只股票: AVG(post_5d) — 历史上榜后平均收益
      - 截面 z-score 标准化
      - 从未上榜股票: 0 (中性)
      - 至少上榜2次才纳入计算

    来源: A股龙虎榜制度独有信号. 历史上榜后持续涨的股票是真正的强势股,
          上榜后持续跌的是散户接盘 (A股实证: 上榜后平均跌0.87%).

    添加: 2026-07-03 — lhb_detail 表补齐后, post_5d 字段已有 24,386 行.
    """
    import sqlite3
    db_path = _market_db_path()
    conn = _db_connect()

    symbols = list(data["close"].columns)

    all_dates = [str(d)[:10] for d in sorted(data.index)]
    date_str = str(date)[:10]
    if date_str not in all_dates:
        conn.close()
        return pd.Series(0.0, index=symbols, name="lhb_post_quality")

    idx = all_dates.index(date_str)
    start_idx = max(0, idx - window)
    end_idx = max(0, idx - 5)  # 排除最近5天 (post_5d 尚未实现)
    if end_idx <= start_idx:
        conn.close()
        return pd.Series(0.0, index=symbols, name="lhb_post_quality")

    start_date = all_dates[start_idx]
    end_date = all_dates[end_idx]

    rows = conn.execute("""
        SELECT symbol, AVG(post_5d) as avg_post5, COUNT(*) as n
        FROM lhb_detail
        WHERE trade_date BETWEEN ? AND ? AND post_5d IS NOT NULL
        GROUP BY symbol
        HAVING n >= 2
    """, (start_date, end_date)).fetchall()
    conn.close()

    scores = {}
    for sym, avg_p5, n in rows:
        if avg_p5 is not None:
            scores[sym] = avg_p5

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("lhb_post_quality")




def compute_margin_balance_chg(data: "pd.DataFrame", date: str, window: int = 5) -> "pd.Series":
    """融资余额变化率: (今日余额 - window日前余额) / window日前余额。

    数据源: margin_detail 表 (融资融券每日明细)
    逻辑: 融资余额增加 → 杠杆资金看多 → 正向预期收益
    实证: A股融资余额变化率与次日收益 IC≈0.03-0.06

    添加: 2026-07-03 — Phase 8 P2, margin_detail 表同步后激活.
    """
    import sqlite3
    db_path = _market_db_path()
    conn = _db_connect()
    symbols = list(data["close"].columns)
    date_str = str(date)[:10]

    all_dates = sorted(data.index)
    idx = None
    for i, d in enumerate(all_dates):
        if str(d)[:10] == date_str:
            idx = i
            break
    if idx is None or idx < window:
        conn.close()
        return pd.Series(0.0, index=symbols, name="margin_balance_chg")

    prev_date = str(all_dates[idx - window])[:10]

    rows = conn.execute("""
        SELECT symbol, margin_balance FROM margin_detail WHERE date IN (?, ?)
    """, (date_str, prev_date)).fetchall()
    conn.close()

    today = {}
    prev = {}
    for sym, bal in rows:
        # Multiple rows possible if sym appears in both dates, need to track which date
        pass

    # Re-query properly
    conn2 = _db_connect()
    today_rows = conn2.execute(
        "SELECT symbol, margin_balance FROM margin_detail WHERE date=?", (date_str,)
    ).fetchall()
    prev_rows = conn2.execute(
        "SELECT symbol, margin_balance FROM margin_detail WHERE date=?", (prev_date,)
    ).fetchall()
    conn2.close()

    today_map = {r[0]: r[1] for r in today_rows if r[1] and r[1] > 0}
    prev_map = {r[0]: r[1] for r in prev_rows if r[1] and r[1] > 0}

    scores = {}
    for sym in symbols:
        t = today_map.get(sym)
        p = prev_map.get(sym)
        if t and p and p > 0:
            scores[sym] = (t - p) / p

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("margin_balance_chg")


def compute_margin_buy_ratio_price(data: "pd.DataFrame", date: str, window: int = 5) -> "pd.Series":
    """融资买入占比: AVG(margin_buy / margin_balance) over window 天。

    数据源: margin_detail 表
    逻辑: 融资买入占余额比高 → 杠杆资金活跃 → 正向预期收益
    实证: 融资买入占比与短期动量正相关 IC≈0.02-0.04

    命名: margin_buy_ratio_5d — 与单日版 margin_buy_ratio 区分, 避免重复注册。
    添加: 2026-07-03 — Phase 8 P2.
    """
    import sqlite3
    db_path = _market_db_path()
    conn = _db_connect()
    symbols = list(data["close"].columns)
    date_str = str(date)[:10]

    dates = []
    for d in sorted(data.index):
        if str(d)[:10] < date_str:
            dates.append(str(d)[:10])
    if len(dates) < window:
        conn.close()
        return pd.Series(0.0, index=symbols, name="margin_buy_ratio_5d")
    lookback_dates = dates[-window:]

    placeholders = ','.join(['?'] * len(lookback_dates))
    rows = conn.execute(f"""
        SELECT symbol, AVG(CASE WHEN margin_balance > 0 THEN margin_buy * 1.0 / margin_balance ELSE NULL END) as avg_ratio
        FROM margin_detail
        WHERE date IN ({placeholders}) AND margin_buy IS NOT NULL AND margin_balance > 0
        GROUP BY symbol
    """, lookback_dates).fetchall()
    conn.close()

    scores = {r[0]: r[1] for r in rows if r[1] is not None}
    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("margin_buy_ratio_5d")
def compute_main_flow_ratio(data: "pd.DataFrame", date: str, window: int = 5) -> "pd.Series":
    """主力资金流向: AVG(main_net_ratio) over window 天。

    数据源: fund_flow 表 (个股资金流向)
    逻辑: 主力净流入占比高 → 聪明钱进场 → 正向预期收益
    实证: 主力资金净流入与短期收益正相关 IC≈0.03-0.05

    添加: 2026-07-03 — Phase 8 P2.
    """
    import sqlite3
    db_path = _market_db_path()
    conn = _db_connect()
    symbols = list(data["close"].columns)
    date_str = str(date)[:10]

    dates = []
    for d in sorted(data.index):
        if str(d)[:10] < date_str:
            dates.append(str(d)[:10])
    if len(dates) < window:
        conn.close()
        return pd.Series(0.0, index=symbols, name="main_flow_ratio")
    lookback_dates = dates[-window:]

    placeholders = ','.join(['?'] * len(lookback_dates))
    rows = conn.execute(f"""
        SELECT symbol, AVG(main_net_ratio) as avg_ratio
        FROM fund_flow
        WHERE date IN ({placeholders}) AND main_net_ratio IS NOT NULL
        GROUP BY symbol
    """, lookback_dates).fetchall()
    conn.close()

    scores = {r[0]: r[1] for r in rows if r[1] is not None}
    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("main_flow_ratio")



def compute_fund_change(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """基金持仓变动: 最新季报的持股变动比例 (change_ratio)。

    数据源: fund_hold 表 (季度基金持仓)
    逻辑: 基金增持 → 机构看好 → 正向预期收益
    实证: 机构持仓变动 IC≈+0.03~0.05
    频率: 季度更新, 窗口=120天(覆盖最近季度+披露滞后)

    添加: 2026-07-03 — Phase 9.
    """
    import sqlite3
    db_path = _market_db_path()
    conn = _db_connect()
    symbols = list(data["close"].columns)
    date_str = str(date)[:10]

    # Find latest quarter end date before this trading date
    rows = conn.execute("""
        SELECT symbol, change_ratio FROM fund_hold
        WHERE report_date = (SELECT MAX(report_date) FROM fund_hold WHERE report_date <= ?)
    """, (date_str,)).fetchall()
    conn.close()

    scores = {}
    for sym, ratio in rows:
        if ratio is not None:
            scores[sym] = float(ratio)

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("fund_change")


def compute_analyst_buy(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """分析师看好度: 买入+增持占全部评级的比例。

    数据源: analyst_forecast 表 (全量分析师预测)
    逻辑: 买入/增持占比高 → 分析师共识看多 → 正向预期收益
    实证: 分析师评级修正 IC≈+0.04~0.07

    添加: 2026-07-03 — Phase 9.
    """
    import sqlite3
    db_path = _market_db_path()
    conn = _db_connect()
    symbols = list(data["close"].columns)
    date_str = str(date)[:10]

    rows = conn.execute("""
        SELECT symbol, buy_count, overweight_count, neutral_count, underweight_count
        FROM analyst_forecast
        WHERE sync_date = (SELECT MAX(sync_date) FROM analyst_forecast WHERE sync_date <= ?)
    """, (date_str,)).fetchall()
    conn.close()

    scores = {}
    for sym, buy, over, neutral, under in rows:
        total = (buy or 0) + (over or 0) + (neutral or 0) + (under or 0)
        if total > 0:
            scores[sym] = ((buy or 0) + (over or 0)) / total

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.5)  # 无数据 -> 中性
    return _cs_zscore(result).rename("analyst_buy")


def _market_db_path():
    return _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "data", "market.db")



def load_active_price_factors(status_filter='active'):
    """从 factor_registry 表加载价格因子 → {name: (cat, window, fn)}.
    
    status_filter: 'active'→active+monitoring (生产), None (全部, 评估用).
    """
    conn = _db_connect()
    name_list = list(_PRICE_FN_MAP.keys())
    placeholders = ",".join("?" * len(name_list))
    if status_filter:
        # 'active' maps to active+monitoring (both are in production)
        statuses = ('active', 'monitoring') if status_filter == 'active' else (status_filter,)
        ph = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT name FROM factor_registry WHERE status IN ({ph}) AND name IN ({placeholders})",
            list(statuses) + name_list
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT name FROM factor_registry WHERE name IN ({placeholders})",
            name_list
        ).fetchall()
    conn.close()
    result = {}
    for (name,) in rows:
        if name in _PRICE_FN_MAP:
            fn, win = _PRICE_FN_MAP[name]
            result[name] = ("dynamic", win, fn)
    return result

def load_active_fundamental_factors(status_filter='active'):
    """从 factor_registry 表加载基本面因子.
    
    status_filter: 'active'→active+monitoring (生产), None (全部, 评估用).
    """
    conn = _db_connect()
    fn_names = list(_FUNDAMENTAL_FN_MAP.keys())
    placeholders = ",".join("?" * len(fn_names))
    if status_filter:
        statuses = ('active', 'monitoring') if status_filter == 'active' else (status_filter,)
        ph = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT name FROM factor_registry WHERE status IN ({ph}) AND name IN ({placeholders})",
            list(statuses) + fn_names
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT name FROM factor_registry WHERE name IN ({placeholders})",
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
    conn = _market_conn("rw")
    conn.execute(
        "UPDATE factor_registry SET ic_mean=?, ic_ir=?, last_evaluated=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE name=?",
        (round(ic_mean, 6), round(ic_ir, 4), name)
    )
    conn.commit()
    conn.close()

# ═══════════════════════════════════════════════════════════


def get_factor_names(status_filter='active') -> list:
    """返回因子名列表 (从 factor_registry 表读取)。

    status_filter: 'active' (生产), None (全部, 评估用).
    """
    price_factors = load_active_price_factors(status_filter)
    fund_factors = load_active_fundamental_factors(status_filter)
    return list(price_factors.keys()) + list(fund_factors.keys())



def compute_all_factors(data: pd.DataFrame, date: str,
                      fundamentals: pd.DataFrame = None,
                      benchmark_ret: Optional["pd.Series"] = None,
                      factor_names: list = None,
                      preloaded_financials: pd.DataFrame = None,
                      preloaded_fundamentals: pd.DataFrame = None) -> dict:
    """批量计算所有已注册因子 → {factor_name: Series(index=symbol)}。

    价格因子从 data 计算, 基本面因子从 fundamentals 计算。
    benchmark_ret 用于特质波动率因子(对指数回归取残差)。
    """
    results = {}
    if factor_names is not None:
        price_factors = {n: ('dynamic', _PRICE_FN_MAP[n][1], _PRICE_FN_MAP[n][0])
                        for n in factor_names if n in _PRICE_FN_MAP}
        fund_factors = {n: _FUNDAMENTAL_FN_MAP[n]
                       for n in factor_names if n in _FUNDAMENTAL_FN_MAP}
    else:
        price_factors = load_active_price_factors()
        fund_factors = load_active_fundamental_factors()

    total_pf = len(price_factors)
    done_pf = 0
    _plog = None
    import time as _time
    _t0 = _time.time()
    for name, (cat, win, fn) in price_factors.items():
        try:
            if _plog is None:
                from utils.logger import get_logger
                _plog = get_logger("factor.compute")
            if 'idio_vol' in name and benchmark_ret is not None:
                results[name] = fn(data, date, win, benchmark_ret=benchmark_ret)
            else:
                _plog.info(f"  computing {name}...")
                results[name] = fn(data, date, win)
        except Exception as e:
            from utils.logger import get_logger
            if _plog is None: _plog = get_logger("factor.compute")
            _plog.warning(f"price factor {name} failed: {e}")
            results[name] = pd.Series(dtype=float)
        done_pf += 1
        if done_pf % 5 == 0 or done_pf == total_pf:
            if _plog is None:
                from utils.logger import get_logger
                _plog = get_logger("factor.compute")
            _plog.info(f"  price factors: {done_pf}/{total_pf} ({done_pf*100//total_pf}%, {_time.time()-_t0:.0f}s)")
    if _plog: _plog.info(f"  price factors done: {total_pf} in {_time.time()-_t0:.0f}s")
    if fundamentals is not None and not fundamentals.empty:
        financials = None
        if fundamentals is not None and any(n in fund_factors for n in _FIN_FACTORS):
            if preloaded_financials is not None:
                # preloaded_financials is dict {date_str: DataFrame}, look up specific date
                financials = preloaded_financials.get(date)
            else:
                from data.store import DataStore
                store = DataStore()
                financials = store.get_financials(fundamentals.index.tolist(), date=date)
                store.close()
        total_ff = len(fund_factors)
        done_ff = 0
        import time as _time2
        _t1 = _time2.time()
        for name, (cat, fn) in fund_factors.items():
            try:
                _plog.info(f"  computing {name}...")
                if name in _FIN_FACTORS and financials is not None:
                    results[name] = fn(fundamentals, date, financials=financials)
                else:
                    results[name] = fn(fundamentals, date)
            except Exception as e:
                from utils.logger import get_logger
                get_logger("factor.compute").warning(f"fundamental factor {name} failed: {e}")
                results[name] = pd.Series(dtype=float)
            done_ff += 1
            if done_ff % 5 == 0 or done_ff == total_ff:
                _plog.info(f"  fundamental factors: {done_ff}/{total_ff} ({done_ff*100//total_ff}%, {_time2.time()-_t1:.0f}s)")
        _plog.info(f"  fundamental factors done: {total_ff} in {_time2.time()-_t1:.0f}s")
    return results

# 7. 基本面因子 — Fama & French (1992, 1993, 2015)
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

def compute_margin_buy_ratio(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """融资买入占余额比: margin_buy / margin_balance (广发证券 2024, IC=-7.95%).

    公式: 融资买入额 / 融资余额。分母是余额而非成交额。
    数据源: margin_detail 表 (akshare stock_margin_detail_sse/szse)。
    来源: 广发证券《多因子ALPHA系列之五十二：基于融资融券因子研究》2024.02。
    """
    conn = _db_connect()
    rows = conn.execute(
        "SELECT symbol, margin_buy, margin_balance FROM margin_detail "
        "WHERE date = (SELECT MAX(date) FROM margin_detail WHERE date <= ?)",
        (date,)
    ).fetchall()
    conn.close()
    if not rows:
        return pd.Series(dtype=float, name="margin_buy_ratio")
    s = pd.Series({r[0]: r[1] / r[2] if r[2] and r[2] > 0 else np.nan
                   for r in rows if r[1] is not None and r[2] is not None})
    return s.dropna().rename("margin_buy_ratio")


def compute_analyst_consensus(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """分析师共识度: buy_count / report_count (盈利预测一致预期)。

    公式: 买入评级数 / 总报告数。值高 = 分析师一致看多。
    数据源: analyst_forecast 表 (akshare stock_analyst_rank_em)。
    来源: 中信建投《逐鹿Alpha》2022, 海通金工 2023。
    """
    conn = _db_connect()
    rows = conn.execute(
        "SELECT symbol, buy_count, report_count FROM analyst_forecast "
        "WHERE sync_date = (SELECT MAX(sync_date) FROM analyst_forecast WHERE sync_date <= ?)",
        (date,)
    ).fetchall()
    conn.close()
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
    return _cs_zscore(result).rename("financial_anomaly")


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

    return _cs_zscore(roe).rename("roe_trimmed")




# ── Phase 4 专项数据源因子 ──

def compute_ihn(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """IHN 持仓机构个数: log(1+fund_count). 来源: 光大 2020, ICIR=0.74."""
    conn = _db_connect()
    rows = conn.execute(
        "SELECT symbol, fund_count FROM fund_hold "
        "WHERE report_date = (SELECT MAX(report_date) FROM fund_hold WHERE report_date <= ?)",
        (date,)
    ).fetchall()
    conn.close()
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
    conn.close()
    if not rows:
        return pd.Series(dtype=float, name="insider_increase")

    increase_vol = pd.Series({r[0]: r[1] for r in rows})

    if fundamentals is not None and not fundamentals.empty and "total_mv" in fundamentals.columns:
        market_cap = fundamentals["total_mv"].fillna(0)
    else:
        conn2 = _db_connect()
        mv_df = pd.read_sql("SELECT symbol, total_mv FROM stocks WHERE total_mv > 0", conn2)
        conn2.close()
        market_cap = mv_df.set_index("symbol")["total_mv"] if not mv_df.empty else pd.Series(dtype=float)

    aligned = increase_vol.index.intersection(market_cap.index)
    if len(aligned) == 0:
        return pd.Series(dtype=float, name="insider_increase")
    ratio = increase_vol[aligned] / market_cap[aligned].replace(0, np.nan)
    ratio = ratio.replace([np.inf, -np.inf], np.nan).dropna()
    return _cs_zscore(ratio).rename("insider_increase")


def compute_earnings_revision(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """盈利修正三组件(简化): net_bull * log(1+report_count). 来源: 华泰 2024, ICIR=2.20."""
    conn = _db_connect()
    rows = conn.execute(
        "SELECT symbol, report_count, buy_count, overweight_count, "
        "neutral_count, underweight_count FROM analyst_forecast "
        "WHERE sync_date = (SELECT MAX(sync_date) FROM analyst_forecast WHERE sync_date <= ?)",
        (date,)
    ).fetchall()
    conn.close()
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
    return _cs_zscore(result).rename("earnings_revision")



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
        conn.close()
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
        from data.store import DataStore
        store = DataStore()
        fin = store.get_financials(fundamentals.index.tolist(), date=date)
        store.close()
    if fin.empty or "net_profit" not in fin.columns or "total_owner_equities" not in fin.columns:
        return pd.Series(np.nan, index=fundamentals.index, name="roe_reported")
    roe = fin["net_profit"] / fin["total_owner_equities"]
    roe = roe.replace([np.inf, -np.inf], np.nan)
    roe = roe.where((roe > -1) & (roe < 1))  # filter extreme
    return _cs_zscore(roe.reindex(fundamentals.index)).rename("roe_reported")


def compute_roa(fundamentals, date, financials=None):
    """ROA = net_profit / total_assets
    来源: Novy-Marx (2013) — 盈利能力
    """
    fin = financials
    if fin is None:
        from data.store import DataStore
        store = DataStore()
        fin = store.get_financials(fundamentals.index.tolist(), date=date)
        store.close()
    if fin.empty or "net_profit" not in fin.columns or "total_assets" not in fin.columns:
        return pd.Series(np.nan, index=fundamentals.index, name="roa")
    roa = fin["net_profit"] / fin["total_assets"]
    roa = roa.replace([np.inf, -np.inf], np.nan)
    roa = roa.where((roa > -0.5) & (roa < 0.5))
    return _cs_zscore(roa.reindex(fundamentals.index)).rename("roa")


def compute_debt_ratio(fundamentals, date, financials=None):
    """资产负债率 = total_liability / total_assets（低分=低负债=好）
    来源: Penman et al. (2007)
    """
    fin = financials
    if fin is None:
        from data.store import DataStore
        store = DataStore()
        fin = store.get_financials(fundamentals.index.tolist(), date=date)
        store.close()
    if fin.empty or "total_liability" not in fin.columns or "total_assets" not in fin.columns:
        return pd.Series(np.nan, index=fundamentals.index, name="debt_ratio")
    dr = fin["total_liability"] / fin["total_assets"]
    dr = dr.replace([np.inf, -np.inf], np.nan)
    dr = dr.where((dr > 0) & (dr < 2))
    # 低负债=高分 (取负号), IC=可能正向(高负债在A股可能预示扩张)
    return _cs_zscore(dr).rename("debt_ratio")


def compute_accruals(fundamentals, date, financials=None):
    """应计利润 = (net_profit - net_operate_cash_flow) / total_assets
    来源: Sloan (1996) — 低应计利润=高质量盈利=未来高收益
    取负号: 低应计→高分
    """
    fin = financials
    if fin is None:
        from data.store import DataStore
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
    return _cs_zscore(-acc).rename("accruals")


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
    from data.store import market_conn
    fin = financials
    if fin is None:
        from data.store import DataStore
        store = DataStore()
        fin = store.get_financials(fundamentals.index.tolist(), date=date)
        store.close()

    if fin.empty or 'total_assets' not in fin.columns:
        return pd.Series(np.nan, index=fundamentals.index, name="asset_growth")

    # 当前季度: fin 已有最新 total_assets
    # 需要去年同期: 查询 financial_balance
    db = _market_db_path()
    conn = _market_conn("ro")

    # 获取每个 symbol 的最新 stat_date
    _syms = fundamentals.index.tolist()
    _ph = ",".join(["?"] * len(_syms))
    rows = conn.execute(f"""
        SELECT symbol, stat_date, total_assets
        FROM financial_balance
        WHERE symbol IN ({_ph})
        ORDER BY stat_date DESC
    """, _syms).fetchall()
    conn.close()

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
    return _cs_zscore(-ag_series).rename("asset_growth")


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
        from data.store import DataStore
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
    return _cs_zscore(gp_ta).rename("gp_ta")


# ═══════════════════════════════════════════════════════════
# 19. 停牌比率 (Zero Trading Days) — Liu (2006)
#    针对中国市场的流动性度量. 比 Amihud 更适配 A 股特征.
#    高停牌比率=流动性差=折价.
# ═══════════════════════════════════════════════════════════

def compute_ztd(data, date, window=250):
    """停牌比率: 过去 window 交易日中零成交天数占比, 取负号.

    Liu (2006): A股停牌是流动性风险的直接度量.
    零成交日=停牌日 (或流动性枯竭). 高分=低停牌=好流动性.

    数据源: daily.volume (日线成交量).
    """
    import sqlite3, pandas as pd

    close = data["close"]
    db = _market_db_path()
    conn = _market_conn("ro")

    # 取过去 window 个日历日在 daily 表中的数据
    _syms = close.columns.tolist()
    _ph = ",".join(["?"] * len(_syms))

    # 计算截止日期: date 往前推 window 个日历日
    end_date = pd.Timestamp(date)
    start_date = end_date - pd.Timedelta(days=int(window * 1.5))  # 日历日覆盖交易日

    rows = conn.execute(f"""
        SELECT date, symbol, volume
        FROM daily
        WHERE date BETWEEN ? AND ?
          AND symbol IN ({_ph})
        ORDER BY date
    """, [start_date.strftime("%Y-%m-%d"), date] + _syms).fetchall()
    conn.close()

    if not rows:
        return pd.Series(np.nan, index=close.columns, name="ztd")

    df = pd.DataFrame(rows, columns=['date', 'symbol', 'volume'])
    # 对每个 symbol, 取最近 window 行 (按日期降序)
    ztd_vals = {}
    for sym in close.columns:
        sym_df = df[df['symbol'] == sym].sort_values('date', ascending=False)
        if len(sym_df) == 0:
            continue
        # 取最近 window 行
        recent = sym_df.head(window)
        zero_days = (recent['volume'] == 0).sum()
        ztd_vals[sym] = zero_days / len(recent) if len(recent) > 0 else np.nan

    ztd = pd.Series(ztd_vals, name="ztd")
    # 高停牌→低流动性→折价: 取负号
    return _cs_zscore(-ztd).rename("ztd")


# ═══════════════════════════════════════════════════════════
# 20. 北向资金净流入 — 沪深港通资金流因子
#    A股验证: 华泰 2023, 中金 2022. 北向资金对次日收益有预测力.
# ═══════════════════════════════════════════════════════════

def compute_northbound_flow(data, date, window=20):
    """北向资金净流入: 过去 window 日累计净买入 / 流通市值.

    来源: 华泰金工 2023 — 北向资金流向对A股有显著预测力.
    高分 = 近期北向资金净流入 → 预期上涨.

    数据源: northbound_flow.net_buy (日频), stocks.total_mv (市值归一化).
    仅覆盖沪深港通标的 (~1500只).
    """
    import sqlite3, pandas as pd

    close = data["close"]
    db = _market_db_path()
    conn = _market_conn("ro")

    _syms = close.columns.tolist()
    _ph = ",".join(["?"] * len(_syms))

    # 查询北向资金
    nb_rows = conn.execute(f"""
        SELECT date, symbol, net_buy
        FROM northbound_flow
        WHERE date <= ? AND symbol IN ({_ph})
        ORDER BY date DESC
    """, [date] + _syms).fetchall()

    # 查询市值 (用于归一化)
    mv_rows = conn.execute(f"""
        SELECT symbol, total_mv FROM stocks
        WHERE symbol IN ({_ph}) AND total_mv IS NOT NULL
    """, _syms).fetchall()
    conn.close()

    mv_map = {r[0]: r[1] for r in mv_rows}
    if not nb_rows:
        return pd.Series(np.nan, index=close.columns, name="northbound_20d")

    nb_df = pd.DataFrame(nb_rows, columns=['date', 'symbol', 'net_buy'])

    nb_vals = {}
    for sym in close.columns:
        sym_df = nb_df[nb_df['symbol'] == sym].sort_values('date', ascending=False)
        if len(sym_df) == 0:
            continue
        recent = sym_df.head(window)
        total_net = recent['net_buy'].sum()
        mv = mv_map.get(sym)
        if mv and mv > 0:
            nb_vals[sym] = total_net / mv
        else:
            # 无市值数据时用原始值 (截面标准化会处理量纲)
            nb_vals[sym] = total_net

    nb = pd.Series(nb_vals, name="northbound_20d")
    nb = nb.replace([np.inf, -np.inf], np.nan)
    # 高净流入→高分
    return _cs_zscore(nb).rename("northbound_20d")





# 需要三表(资产负债表+利润表+现金流量表)合并数据的因子名
# 模板 2a: 这些因子接收 financials=DataFrame 参数, 不内部访问 DataStore
# 函数定义在文件上方, 此处位于 compute_all_factors 之后, 确保函数已定义


# ═══════════════════════════════════════════════════════════
# 21. SUE (标准化未预期盈余) — Bernard & Thomas (1989) PEAD
#    A股验证: 中信 2022. 季报盈余超预期→公告后漂移.
#    SUE = (EPS_t - EPS_{t-4q}) / σ(EPS_8q), 取正号 (高SUE→高分).
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
    conn = _market_conn("ro")

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
    conn.close()

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
    return _cs_zscore(sue_series).rename("sue")




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
    from data.store import market_conn

    db = _market_db_path()
    conn = _market_conn("ro")

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
    conn.close()

    vals = {r[0]: r[1] for r in rows if r[1] is not None}
    result = pd.Series(vals, name="holder_reduction")
    result = result.replace([float('inf'), float('-inf')], float('nan'))
    # 高减持→低分 (IC为负)
    # 注: akshare 只返回绝对股数, 横截面 z-score 标准化已处理量纲差异
    return _cs_zscore(-result).rename("holder_reduction")


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
    from data.store import market_conn

    db = _market_db_path()
    conn = _market_conn("ro")

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
    conn.close()

    vals = {}
    for r in rows:
        if r[1] and r[2] and r[2] > 0:
            vals[r[0]] = r[1] / r[2]

    result = pd.Series(vals, name="pledge_ratio")
    result = result.clip(0, 1)
    # 高质押→低分
    return _cs_zscore(-result).rename("pledge_ratio")


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
    from data.store import market_conn

    db = _market_db_path()
    conn = _market_conn("ro")

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
    conn.close()

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
    return _cs_zscore(result).rename("dividend_yield")




# ═══════════════════════════════════════════════════════════
# P70: 四新因子 — OIR 昼夜 / STR 量稳 / ABN_TURN 残差 / OCFP 现金流
# 来源: 2021-2026 券商金工研报系统搜索, docs/research/四因子接入分析_2026-07-07.md
# ═══════════════════════════════════════════════════════════

def compute_day_night(data, date, night_window=10, intraday_window=20):
    """OIR 昼夜合成因子: 0.6×日内反转 + 0.4×隔夜跳空绝对值.
    
    华安证券(2020): 《昼夜分离，隔夜跳空与日内反转选股因子》.
    IC=-8.1%, ICIR=4.04, 月度胜率 89.6%.
    逻辑: T+1制度下日内收益反转(涨→跌), 隔夜跳空绝对值越大→未来收益越低.
    仅需日频 OHLC, akshare 免费.

    Args:
        night_window: 隔夜跳空回看窗口 (默认10日, 衰减快)
        intraday_window: 日内反转回看窗口 (默认20日, 衰减慢)
    """
    import numpy as np
    close = data["close"]
    open_ = data["open"]

    # 日内反转: 累计对数收益率, 取负 (IC为负)
    ret_intra = np.log(close / open_)
    intra_rev = ret_intra.rolling(intraday_window, min_periods=10).sum()

    # 隔夜跳空: 绝对值累计 (无论高开低开, 跳空幅度大→次月反转)
    ret_night = np.log(open_ / close.shift(1))
    night_jump = ret_night.abs().rolling(night_window, min_periods=5).sum()

    raw = 0.6 * intra_rev.iloc[-1] + 0.4 * night_jump.iloc[-1]
    # 取负: 因子值越小 (越负) → 买入信号越强
    return _cs_zscore(-raw).rename("day_night")


def compute_str(data, date, window=20):
    """STR 量稳换手率: 过去 window 日换手率的标准差, 取负, 市值中性化.
    
    东吴证券(2021): 《量稳换手率选股因子——量小、量缩，都不如量稳？》.
    IC=-7.9%, IR=2.96, 胜率 77.6%.
    逻辑: 换手率波动大→未来收益低, 稳定性比绝对水平更有预测力.
    仅需日频换手率.

    Args:
        window: 标准差回看窗口 (默认20日, 匹配月频调仓; 10-60日均稳健)
    """
    import sqlite3, numpy as np, os
    close = data["close"]
    syms = close.columns.tolist()
    end_date = close.index[-1].strftime("%Y-%m-%d") if hasattr(close.index[-1], 'strftime') else str(close.index[-1])[:10]

    # 从 daily 表读取换手率 (get_daily 不含 turnover 字段)
    db = _market_db_path()
    conn = _market_conn("ro")
    _ph = ",".join(["?"] * len(syms))
    rows = conn.execute(f"""
        SELECT date, symbol, turnover FROM daily
        WHERE date <= ? AND symbol IN ({_ph})
        ORDER BY date
    """, [end_date] + syms).fetchall()
    conn.close()

    if not rows:
        return pd.Series(np.nan, index=close.columns, name="str")

    df = pd.DataFrame(rows, columns=['date', 'symbol', 'turnover'])
    df['date'] = pd.to_datetime(df['date'])

    # 用 groupby 替代逐只循环 — O(n) vs O(n²)
    df = df.sort_values(['symbol', 'date'])
    df['_rn'] = df.groupby('symbol').cumcount(ascending=False)  # 倒序行号
    recent = df[df['_rn'] < window]
    raw = recent.groupby('symbol')['turnover'].std()
    counts = df.groupby('symbol').size()
    min_records = max(window // 2, 10)
    raw = raw[counts >= min_records]
    raw = raw.dropna()
    raw.name = 'str'
    if raw.empty or raw.count() < 30:
        return _cs_zscore(-raw).rename("str")

    # 市值中性化 (从 stocks 表取 total_mv)
    try:
        conn2 = _market_conn("ro")
        _syms2 = raw.index.tolist()
        _ph2 = ",".join(["?"] * len(_syms2))
        rows = conn2.execute(
            f"SELECT symbol, total_mv FROM stocks WHERE symbol IN ({_ph2}) AND total_mv IS NOT NULL",
            _syms2
        ).fetchall()
        conn2.close()
        mv_map = {r[0]: r[1] for r in mv_rows}
        log_mv = pd.Series({s: np.log(mv_map[s]) for s in raw.index if s in mv_map})
        common = raw.index.intersection(log_mv.index)
        if len(common) >= 30:
            from sklearn.linear_model import LinearRegression
            X = log_mv.loc[common].values.reshape(-1, 1)
            y = raw.loc[common].values
            resid = y - LinearRegression().fit(X, y).predict(X)
            raw = pd.Series(resid, index=common)
    except Exception:
        pass

    # 取负: 低波动→高分
    return _cs_zscore(-raw).rename("str")


def compute_abn_turnover(data, date, window=20):
    """ABN_TURN 异常换手率残差: 对 ln(Turnover) 做市值+行业回归取残差, 取负.
    
    Chordia, Huh & Subrahmanyam (2007, JFE); 东方证券朱剑涛(2015)首次引入A股.
    IC=-6.77%, 与 STR 相关 0.3-0.5, 互补.
    逻辑: 剔除市值和行业效应后的"真正异常"换手率 → 异常高换手→反转下跌.
    仅需日频换手率+市值+行业分类.

    Args:
        window: 换手率均值窗口 (默认20日)
    """
    import sqlite3, numpy as np
    close = data["close"]
    syms = close.columns.tolist()
    end_date = close.index[-1].strftime("%Y-%m-%d") if hasattr(close.index[-1], 'strftime') else str(close.index[-1])[:10]

    db = _market_db_path()
    conn = _market_conn("ro")
    _ph = ",".join(["?"] * len(syms))

    # 取换手率
    rows = conn.execute(f"""
        SELECT date, symbol, turnover FROM daily
        WHERE date <= ? AND symbol IN ({_ph})
        ORDER BY date
    """, [end_date] + syms).fetchall()

    # 取市值 + 行业
    meta_rows = conn.execute(f"""
        SELECT symbol, total_mv, industry FROM stocks
        WHERE symbol IN ({_ph})
    """, syms).fetchall()
    conn.close()

    mv_map = {r[0]: r[1] for r in meta_rows if r[1]}
    ind_map = {r[0]: r[2] for r in meta_rows if r[2]}

    if not rows:
        return pd.Series(np.nan, index=close.columns, name="abn_turnover")

    df = pd.DataFrame(rows, columns=['date', 'symbol', 'turnover'])
    df['date'] = pd.to_datetime(df['date'])

    # 用 groupby 替代逐只循环 — O(n) vs O(n²)
    df = df.sort_values(['symbol', 'date'])
    df['_rn'] = df.groupby('symbol').cumcount(ascending=False)
    recent = df[df['_rn'] < window]
    avg_turn = recent.groupby('symbol')['turnover'].mean()
    counts = df.groupby('symbol').size()
    min_records = max(window // 2, 10)
    avg_turn = avg_turn[(avg_turn > 0) & (counts >= min_records)]
    turn_series = np.log(avg_turn)
    turn_series.name = 'ln_turnover'
    if turn_series.empty or turn_series.count() < 30:
        return _cs_zscore(-turn_series).rename("abn_turnover")

    # OLS: ln(Turnover) ~ ln(MktCap) + industry dummies
    common = [s for s in turn_series.index if s in mv_map]
    if len(common) < 30:
        return _cs_zscore(-turn_series).rename("abn_turnover")

    from sklearn.linear_model import LinearRegression
    import numpy as np
    y = turn_series.loc[common].values
    log_mv = np.log([mv_map[s] for s in common])
    # 行业哑变量 (只保留有 ≥3 只股票的行业)
    industries = [ind_map.get(s, '') for s in common]
    ind_counts = pd.Series(industries).value_counts()
    valid_inds = ind_counts[ind_counts >= 3].index.tolist()
    ind_dummies = pd.get_dummies(industries)
    valid_cols = [c for c in ind_dummies.columns if c in valid_inds and c != '']
    if valid_cols:
        X = np.column_stack([log_mv, ind_dummies[valid_cols].values])
    else:
        X = log_mv.reshape(-1, 1)

    try:
        resid = y - LinearRegression().fit(X, y).predict(X)
        raw = pd.Series(resid, index=common)
    except Exception:
        raw = turn_series.loc[common]

    # 取负: 异常高换手→低分
    return _cs_zscore(-raw).rename("abn_turnover")


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
    exclude_inds = {'银行', '非银金融', '房地产', '综合金融'}
    valid_syms = [s for s in syms if s in mv_map and ind_map.get(s, '') not in exclude_inds]

    # TTM经营现金流: 直接查 financial_cash_flow 表最近4个季度
    ocfp_vals = {}
    try:
        _conn = _market_conn("ro")
        placeholders = ",".join("?" for _ in valid_syms)
        cf_df = pd.read_sql_query(
            f"""SELECT symbol, stat_date, net_operate_cash_flow
                FROM financial_cash_flow
                WHERE stat_date >= date(?, '-1 year')
                  AND symbol IN ({placeholders})
                ORDER BY symbol, stat_date""",
            _conn, params=[date] + valid_syms
        )
        _conn.close()
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
    except Exception as _e:
        import sys as _sys
        _sys.stderr.write(f"[WORKER-PROC] ocfp TTM query failed: {_e}\n")
        _sys.stderr.flush()

    raw = pd.Series(ocfp_vals, name="ocfp")
    if raw.empty or raw.count() < 30:
        return raw

    # 行业中性化
    try:
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
    except Exception:
        pass

    # 正向: 高OCFP→高分
    return _cs_zscore(raw).rename("ocfp")



# ═══════════════════════════════════════════════════════════
# P71: 涨跌停制度特有效因子 — 封成比 / 封板时间 / 涨停打开 / 净涨停占比
# ═══════════════════════════════════════════════════════════

_shared_limit_conn = None  # 模块级共享连接, 避免每个因子重复开 DB

def _get_limit_pool(date_str: str, conn=None):
    """读取 limit_up_pool 当日数据, 返回 (df_up, df_down) 或 (空df, 空df).

    优先使用传入 conn, 否则回退共享连接, 最后才开新连接.
    """
    own = False
    if conn is None:
        if _shared_limit_conn is not None:
            conn = _shared_limit_conn
        else:
            conn = _db_connect()
            own = True
    df_up = pd.read_sql_query(
        "SELECT * FROM limit_up_pool WHERE date=?", conn, params=(date_str,)
    )
    try:
        df_down = pd.read_sql_query(
            "SELECT * FROM limit_down_pool WHERE date=?", conn, params=(date_str,)
        )
    except Exception:
        df_down = pd.DataFrame()
    if own:
        conn.close()
    return df_up, df_down


def compute_seal_turnover_ratio(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """封成比: lock_capital / amount — 涨停封单金额与成交额之比.

    来源: 国金证券(2016), 华安证券(2026).
    实证: >10→连板概率>60%; <1→警惕炸板. 正向因子(封成比大→买入).
    """
    date_str = str(date)[:10]
    df_up, _ = _get_limit_pool(date_str)
    symbols_all = list(data["close"].columns)

    if df_up.empty:
        return pd.Series(0.0, index=symbols_all, name="seal_turnover_ratio")

    df_up = df_up.set_index("symbol")
    result = pd.Series(0.0, index=symbols_all)
    for sym in df_up.index.intersection(symbols_all):
        row = df_up.loc[sym]
        lock_cap = float(row.get("lock_capital", 0) or 0)
        amount = float(row.get("amount", 0) or 0)
        if amount > 0 and lock_cap > 0:
            result[sym] = lock_cap / amount

    return _cs_zscore(result).rename("seal_turnover_ratio")


def compute_seal_time(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """封板时间: 归一化首次涨停时间, 早封板=高分.

    来源: 国金证券(2016) — 封板时间与次日涨幅严格单调递减.
    公式: 1 - (first_time_min - 570) / 330 (9:30=570min, 15:00=900min)
    """
    date_str = str(date)[:10]
    df_up, _ = _get_limit_pool(date_str)
    symbols_all = list(data["close"].columns)

    if df_up.empty:
        return pd.Series(0.0, index=symbols_all, name="seal_time")

    df_up = df_up.set_index("symbol")
    result = pd.Series(0.0, index=symbols_all)
    for sym in df_up.index.intersection(symbols_all):
        row = df_up.loc[sym]
        ft = row.get("first_time", None)
        if ft is None or str(ft) == "nan" or str(ft) == "":
            continue
        t = str(ft).strip()
        parts = t.split(":")
        if len(parts) < 2:
            continue
        minutes = int(parts[0]) * 60 + int(parts[1])
        if minutes >= 570:  # 不早于 9:30
            result[sym] = 1.0 - (minutes - 570) / 330.0

    return _cs_zscore(result).rename("seal_time")


def compute_limit_touch_no_seal(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """触板未封: high >= pre_close*1.10*0.995 AND ret < 9.5% → 负信号.

    来源: 东方证券 / 涨跌停溢出效应研究 — 触板未封 = 假突破, 次日往往回落.
    """
    date_str = str(date)[:10]
    symbols_all = list(data["close"].columns)
    result = pd.Series(0.0, index=symbols_all)

    if "high" not in data.columns or "close" not in data.columns:
        return result.rename("limit_touch_no_seal")

    try:
        today = data.loc[date_str]
    except KeyError:
        return result.rename("limit_touch_no_seal")

    for sym in symbols_all:
        try:
            # today is a Series with MultiIndex keys like ("high","000001"), ("close","000001")
            if ("high", sym) not in today.index or ("close", sym) not in today.index:
                continue
            high = float(today[("high", sym)])
            close = float(today[("close", sym)])
            # pre_close = yesterday's close from data
            # data has date index, find yesterday
            close_df = data["close"]
            date_idx = close_df.index.get_loc(date_str)
            if date_idx == 0:
                continue
            pre = float(close_df.iloc[date_idx - 1].get(sym, None) or close_df.iloc[date_idx - 1].loc[sym] if sym in close_df.iloc[date_idx - 1].index else None)
            if pre is None or pre <= 0:
                continue
            limit_price = pre * 1.10
            ret = (close - pre) / pre
            if high >= limit_price * 0.995 and ret < 0.095:
                result[sym] = -1.0
        except (KeyError, ValueError, TypeError, IndexError):
            continue

    return _cs_zscore(result).rename("limit_touch_no_seal")


def compute_net_limit_ratio(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """净涨停占比: (n_up - n_down) / n_total — 市场情绪代理.

    来源: 开源证券 / DL合成因子 — 行业内涨跌停股净占比反映情绪溢出.
    """
    date_str = str(date)[:10]
    df_up, df_down = _get_limit_pool(date_str)
    symbols_all = list(data["close"].columns)

    if df_up.empty and df_down.empty:
        return pd.Series(0.0, index=symbols_all, name="net_limit_ratio")

    up_symbols = set(df_up["symbol"].tolist()) if not df_up.empty else set()
    down_symbols = set(df_down["symbol"].tolist()) if not df_down.empty else set()

    total = max(len(up_symbols) + len(down_symbols), 1)
    net = (len(up_symbols) - len(down_symbols)) / total

    result = pd.Series(float(net), index=symbols_all)
    return _cs_zscore(result).rename("net_limit_ratio")


# ═══════════════════════════════════════════════════════════
# P72: 数据源适配因子 — EPA估值异常 / TRCF换手率收敛 / 理想振幅
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
    return _cs_zscore(result).rename("epa")


def compute_trcf(data: "pd.DataFrame", date: str, window: int = 120) -> "pd.Series":
    """TRCF换手率收敛: -log(1 + std(MA5/10/20/60/120 turnover)).

    来源: 数据源适配报告 — ICIR=4.19, turnover 类最强.
    """
    symbols_all = list(data["close"].columns)

    if "turnover" not in data.columns:
        return pd.Series(0.0, index=symbols_all, name="trcf")

    turnover = data["turnover"]
    if date not in turnover.index:
        return pd.Series(0.0, index=symbols_all, name="trcf")

    windows = [5, 10, 20, 60, 120]
    result = pd.Series(0.0, index=symbols_all)

    for sym in symbols_all:
        try:
            if sym not in turnover.columns:
                continue
            ts = turnover[sym].dropna()
            if len(ts) < 120:
                continue
            mas = [ts.tail(w).mean() for w in windows]
            std_ma = np.std(mas)
            result[sym] = -np.log(1 + std_ma)
        except Exception:
            continue

    return _cs_zscore(result).rename("trcf")


def compute_ideal_amplitude(data: "pd.DataFrame", date: str, window: int = 20) -> "pd.Series":
    """理想振幅: -(avg(high 25% amp) - avg(low 25% amp)).

    来源: 开源证券 — ICIR~3.0, 波动率类最强.
    """
    symbols_all = list(data["close"].columns)

    if "high" not in data.columns or "low" not in data.columns:
        return pd.Series(0.0, index=symbols_all, name="ideal_amplitude")

    result = pd.Series(0.0, index=symbols_all)

    for sym in symbols_all:
        try:
            if sym not in data["high"].columns or sym not in data["low"].columns:
                continue
            high = data["high"][sym].dropna()
            low = data["low"][sym].dropna()
            ampl = (high - low) / low
            ampl = ampl.dropna()
            if len(ampl) < window:
                continue
            recent = ampl.tail(window)
            high_q = recent.nlargest(max(int(len(recent) * 0.25), 1)).mean()
            low_q = recent.nsmallest(max(int(len(recent) * 0.25), 1)).mean()
            result[sym] = -(high_q - low_q)
        except Exception:
            continue

    return _cs_zscore(result).rename("ideal_amplitude")


# ══════════════════════════════════════════════════════════════
# P69: Factor maps moved to end of file
# (entries reference functions defined above, forward-reference safe)
# ══════════════════════════════════════════════════════════════

_PRICE_FN_MAP = {
    "reversal_5d":           (compute_reversal,            5),
    "turnover_rev_5d":       (compute_turnover_reversal,   5),
    "max_ret_20d":           (compute_max_return,         20),
    "gap_5d":                (compute_overnight_gap,       5),
    "range_20d":             (compute_intraday_range,     20),
    "momentum_63d":          (compute_momentum,           63),
    "residual_momentum_126d": (compute_residual_momentum,  126),  # Ch.3.7 Kakushadze & Serur 2018
    "momentum_126d":         (compute_momentum,          126),
    "momentum_252d":         (compute_momentum,          252),
    "volatility_126d":       (compute_volatility,        _VOLATILITY_WINDOW),
    "skewness_60d":          (compute_skewness,          _SKEWNESS_WINDOW),
    "idio_vol_126d":         (compute_idiosyncratic_vol, _IDIO_VOL_WINDOW),
    "amihud_250d":           (compute_amihud,            _AMIHUD_WINDOW),
    "rsi_rev_14d":           (compute_rsi_reversal,       14),
    "money_flow_5d":         (compute_money_flow,          5),
    "ma_alignment_20d":      (compute_ma_alignment,       20),
    "vol_price_corr_10d":    (compute_volume_price_corr,  10),
    "turnover_anomaly":      (compute_turnover_anomaly,    5),
    "limit_up_prox_5d":      (compute_limit_up_proximity,  5),
    "zt_streak":             (compute_limit_up_streak,     0),
    "dt_streak":             (compute_dt_streak,          0),
    "lhb_net_buy_20d":       (compute_lhb_net_buy,        20),
    "lhb_post_quality":      (compute_lhb_post_quality,   90),
    "margin_balance_chg":     (compute_margin_balance_chg, 5),
    "margin_buy_ratio_5d":    (compute_margin_buy_ratio_price,   5),
    "fund_change":             (compute_fund_change,        0),
    "analyst_buy":             (compute_analyst_buy,        0),
    # P69: 集中化 — 从动态注册迁移到静态 map
    "ztd":                    (compute_ztd,               250),
    "northbound_20d":         (compute_northbound_flow,    20),
    "day_night":              (compute_day_night,          20),
    "str":                    (compute_str,                20),
    "abn_turnover":           (compute_abn_turnover,       20),
    "seal_turnover_ratio":    (compute_seal_turnover_ratio, 1),
    "seal_time":              (compute_seal_time,           1),
    "limit_touch_no_seal":    (compute_limit_touch_no_seal, 1),
    "net_limit_ratio":        (compute_net_limit_ratio,     1),
    "trcf":                   (compute_trcf,              120),
    "ideal_amplitude":        (compute_ideal_amplitude,     20),
}

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
}
