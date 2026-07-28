"""市场微观结构模型 — bid-ask spread + 流动性估计。

来源:
  Roll(1984): "A Simple Implicit Measure of the Effective Bid-Ask Spread"
    2 × √(-cov(Δp_t, Δp_{t-1})) — 从日线价格序列反推有效价差
  Harris(2003): "Trading and Exchanges" — 市场微观结构理论

使用: 仅在回测/诊断中估算, 不用于实盘下单 (实盘已有五档盘口实时数据)
"""

import numpy as np
import pandas as pd
from quant.data.repos._base import DatabaseManager, query_all
from quant.utils.logger import get_logger

_log = get_logger("execution.microstructure")


def estimate_roll_spread(prices: "pd.Series | np.ndarray") -> float:
    """Roll(1984) 有效价差估计 — 从价格序列协方差反推.

    Roll 模型: spread = 2 × √(-cov(Δp_t, Δp_{t-1}))
    当 cov ≤ 0 时 → 噪声 (无有效信号) → 返回 0.001 (10bp, 默认最小价差)

    Args:
        prices: 日线收盘价序列 (按时间升序)

    Returns:
        估计的有效价差 (比例, e.g. 0.002 = 20bp)
    """
    if isinstance(prices, pd.Series):
        prices = prices.values
    prices = np.asarray(prices, dtype=float)
    if len(prices) < 3:
        return 0.001  # 数据不足, 回退默认

    dp = np.diff(prices) / prices[:-1]  # 日收益率
    if len(dp) < 3:
        return 0.001

    # 序列协方差: cov(Δp_t, Δp_{t-1})
    cov = np.cov(dp[1:], dp[:-1])[0, 1]
    if cov >= 0:
        return 0.001  # 噪声或无有效信号

    spread = 2.0 * np.sqrt(-cov)
    return float(np.clip(spread, 0.0001, 0.05))  # 夹在 1bp ~ 5%


def estimate_effective_spread(symbol: str, date: str, lookback: int = 60) -> dict:
    """从 market.db 获取价格序列, 估算单只股票的 Roll 价差.

    Args:
        symbol: 股票代码
        date: 截止日期 YYYY-MM-DD
        lookback: 回看天数 (默认 60 个交易日)

    Returns:
        {symbol, date, roll_spread_bp, n_days, error}
    """
    try:
        conn = DatabaseManager.market()
        try:
            rows = query_all(conn,
                "SELECT close FROM daily WHERE symbol=? AND date <= ? ORDER BY date DESC LIMIT ?",
                (symbol, date, lookback + 1))
        finally:
            conn.close()

        if not rows or len(rows) < 3:
            return {"symbol": symbol, "date": date, "roll_spread_bp": None,
                    "n_days": len(rows) if rows else 0,
                    "error": "insufficient data"}

        prices = pd.Series([r[0] for r in reversed(rows)], dtype=float)
        spread = estimate_roll_spread(prices)
        return {"symbol": symbol, "date": date,
                "roll_spread_bp": round(spread * 10000, 1),  # bp
                "n_days": len(rows), "error": None}

    except Exception as e:
        return {"symbol": symbol, "date": date, "roll_spread_bp": None,
                "n_days": 0, "error": str(e)[:100]}


def batch_roll_spread(symbols: list[str], date: str, lookback: int = 60) -> "pd.DataFrame":
    """批量估算 Roll 价差, 用于回测前市场微观结构分析.

    Returns:
        DataFrame: columns=[symbol, roll_spread_bp, n_days]
    """
    results = []
    for i, sym in enumerate(symbols):
        r = estimate_effective_spread(sym, date, lookback)
        results.append(r)
        if (i + 1) % 100 == 0:
            _log.debug(f"roll spread: {i+1}/{len(symbols)}")

    df = pd.DataFrame(results)
    _log.info(f"roll spread: {len(symbols)} symbols, "
              f"median={df['roll_spread_bp'].median():.0f}bp, "
              f"null={df['roll_spread_bp'].isna().sum()}")
    return df
