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
    
    results = {}
    for sym in close.columns:
        p = close[sym].iloc[start:idx + 1]
        a = amount[sym].iloc[start:idx + 1]
        if p.count() < window * 0.5 or a.count() < window * 0.5:
            results[sym] = np.nan
            continue
        # 日收益绝对值
        ret = p.pct_change().abs()
        # dollar_volume = amount(千元) × 1000 = 元
        dollar_vol = a * 1000
        illiq = (ret / dollar_vol.replace(0, np.nan)).mean() * 1e6
        results[sym] = illiq
    
    series = pd.Series(results)
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
    # 负偏度 → 高分 (premium for negative skewness)
    return _cs_zscore(skew).rename(f"skewness_{window}d")  # A股正偏度溢价: IC=-0.016→取+skew


# ═══════════════════════════════════════════════════════════
# 因子注册表 — 供 FactorEvaluator 扫描
# ═══════════════════════════════════════════════════════════

# 因子注册表 — 仅保留经 IC/IR 验证有效的 4 因子 (2025Q1-2026Q2 截面评估).
# 已移除 7 个无效/冗余因子: momentum_5d, momentum_60d, reversal_5d,
#   downside_vol_20d, vol_ratio_5d, turnover_chg_5d, amihud_20d.
# 因子注册表 — 经 IC 分析精简 (2025 Dec-2026 Jan 截面)
#  保留正向因子: momentum_10d(+0.017), volatility_20d(+0.034), roe_ratio(+0.032)
#  已移除反向因子: bp_ratio(-0.032→取-方向保留), size(-0.126→移除)
#  共 5 因子 → equal_weight 合成
FACTOR_REGISTRY = {
    "momentum_10d":     ("momentum",  10, compute_momentum),
    "volatility_20d":   ("volatility",20, compute_volatility),
    "skewness_20d":     ("skewness",  20, compute_skewness),
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
    return _cs_zscore(-bp).rename("bp_ratio")  # IC=-0.032: 取-reverse方向


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
    # "bp_ratio": ("value",         compute_bp_ratio),    # IC=-0.032 移除
    # "size":    ("market_cap",    compute_size),        # IC=-0.126 移除
    "roe_ratio":  ("profitability", compute_roe_ratio),
}

def get_factor_names() -> list:
    """返回所有已注册因子名 (价格 + 基本面)。"""
    return list(FACTOR_REGISTRY.keys()) + list(FUNDAMENTAL_FACTOR_REGISTRY.keys())


def compute_all_factors(data: pd.DataFrame, date: str,
                      fundamentals: pd.DataFrame = None) -> dict:
    """批量计算所有已注册因子 → {factor_name: Series(index=symbol)}。
    
    价格因子从 data 计算, 基本面因子从 fundamentals 计算。
    """
    results = {}
    for name, (cat, win, fn) in FACTOR_REGISTRY.items():
        try:
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
