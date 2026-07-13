"""市场冲击模型 — Almgren-Chriss 线性冲击, 替换固定 0.1% 滑点。

来源: Almgren & Chriss (2001) "Optimal Execution of Portfolio Transactions".
A股校准: η (冲击系数) ≈ 0.1~0.3, γ (平方根幂) ≈ 0.5.

公式:
  冲击成本(pct) = η × σ_daily × sqrt(Q / V_daily)
  冲击成本(元)  = 冲击成本(pct) × P × Q

其中:
  σ_daily = 日波动率 (年化波动率 / sqrt(252))
  Q       = 订单股数
  V_daily = 20日均成交量
  η       = 校准系数 (A股 ≈ 0.2)
  P       = 成交价
"""

import numpy as np
import pandas as pd
import sqlite3, os

from utils.logger import get_logger
from config.constants import _require_cfg

_log = get_logger("execution.impact")

_MARKET_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")


def estimate_impact_pct(
    shares: int,
    daily_volume: float,
    daily_volatility: float = None,
    eta: float = None,
    gamma: float = 0.5,
) -> float:
    """估算单笔交易的冲击成本百分比.

    Args:
        shares: 委托股数
        daily_volume: 该股票近 20 日日均成交量 (股)
        daily_volatility: 日波动率 (标准差), 默认从 config 取值
        eta: A股冲击系数, 默认从 config 读取 execution.impact_eta
        gamma: 平方根幂, 默认 0.5

    Returns:
        冲击成本比例 (0.001 = 0.1%)
    """
    if eta is None:
        eta = _require_cfg("execution.impact_eta")
    if daily_volatility is None:
        daily_volatility = _require_cfg("execution.default_daily_vol")

    if daily_volume <= 0:
        _log.warning(f"daily_volume <= 0, using default impact")
        return _require_cfg("execution.slippage")

    # 参与率 (订单量 / 日均量)
    participation = shares / daily_volume

    # Almgren-Chriss: σ × sqrt(Q/V) × η
    impact = daily_volatility * (participation ** gamma) * eta

    # 上限: 不超过 5% (极端小盘股)
    impact = min(impact, 0.05)

    return impact


def estimate_impact_value(
    shares: int,
    price: float,
    daily_volume: float,
    daily_volatility: float = None,
) -> float:
    """估算单笔交易的冲击成本金额.

    Returns the estimated cost in RMB.
    """
    impact_pct = estimate_impact_pct(shares, daily_volume, daily_volatility)
    return price * shares * impact_pct


def get_stock_volume_snapshot(
    symbols: list[str],
    date: str,
    lookback: int = 20,
) -> dict[str, float]:
    """从 market.db 获取多只股票的近 N 日均成交量.

    Args:
        symbols: 股票代码列表
        date: 截止日期 (YYYY-MM-DD)
        lookback: 回顾天数

    Returns:
        {symbol: avg_daily_volume}
    """
    conn = sqlite3.connect(_MARKET_DB)
    result = {}

    try:
        chunk_size = 500
        syms = list(symbols)
        for i in range(0, len(syms), chunk_size):
            chunk = syms[i:i + chunk_size]
            placeholders = ", ".join("?" * len(chunk))
            from_date = pd.Timestamp(date) - pd.Timedelta(days=lookback * 2)
            from_str = from_date.strftime("%Y-%m-%d")
            rows = conn.execute(
                f"SELECT symbol, AVG(volume) FROM daily "
                f"WHERE symbol IN ({placeholders}) AND date BETWEEN ? AND ? "
                f"GROUP BY symbol",
                chunk + [from_str, date]
            ).fetchall()
            for r in rows:
                if r[1] and r[1] > 0:
                    result[r[0]] = float(r[1])
    except Exception as e:
        raise  # 错误不吞
        _log.warning(f"get_stock_volume_snapshot({date}): {e}")
    finally:
        conn.close()

    return result


def get_stock_volatility_snapshot(
    symbols: list[str],
    date: str,
    window: int = 20,
) -> dict[str, float]:
    """从 market.db 获取多只股票的近 N 日波动率.

    Returns:
        {symbol: daily_volatility (std of daily returns)}
    """
    conn = sqlite3.connect(_MARKET_DB)
    result = {}

    try:
        chunk_size = 500
        syms = list(symbols)
        for i in range(0, len(syms), chunk_size):
            chunk = syms[i:i + chunk_size]
            placeholders = ", ".join("?" * len(chunk))
            from_date = pd.Timestamp(date) - pd.Timedelta(days=window * 3)
            from_str = from_date.strftime("%Y-%m-%d")
            rows = conn.execute(
                f"SELECT symbol, close FROM daily "
                f"WHERE symbol IN ({placeholders}) AND date BETWEEN ? AND ? "
                f"ORDER BY symbol, date",
                chunk + [from_str, date]
            ).fetchall()

            # Group by symbol, compute returns
            data = {}
            for sym, close in rows:
                if close and close > 0:
                    data.setdefault(sym, []).append(close)

            for sym, closes in data.items():
                if len(closes) >= window + 1:
                    rets = np.diff(np.log(closes))
                    result[sym] = float(np.std(rets))
    except Exception as e:
        raise  # 错误不吞
        _log.warning(f"get_stock_volatility_snapshot({date}): {e}")
    finally:
        conn.close()

    return result
