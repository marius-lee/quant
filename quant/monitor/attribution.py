"""绩效归因 — Brinson 分解 + 因子暴露分解。"""
from quant.utils.logger import get_logger
logger = get_logger("monitor.attribution")
#
# 将组合总收益分解为:
#   配置收益 (Allocation): 组合行业权重 vs 基准行业权重的差异带来的收益
#   选股收益 (Selection): 行业内个股选择带来的收益
#   交互效应 (Interaction): 配置与选股的交叉项
#
# 来源: ② Brinson, Hood & Beebower (1986) — 绩效归因经典框架

import numpy as np
import pandas as pd
from typing import Optional

from quant.config.constants import _require_cfg
_RF_RATE = _require_cfg("attribution.risk_free_rate")       # from config
_ANNUAL_PERIODS = _require_cfg("attribution.annual_periods")  # from config


def brinson_attribution(
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
    portfolio_weights: pd.Series,
    benchmark_weights: pd.Series,
) -> dict:
    """Brinson 归因: 总收益 = 配置 + 选股 + 交互。

    portfolio_returns: index=sector, 组合在各行业的收益
    benchmark_returns: index=sector, 基准在各行业的收益
    portfolio_weights: index=sector, 组合行业权重
    benchmark_weights: index=sector, 基准行业权重

    返回: {"allocation": float, "selection": float, "interaction": float, "total": float}
    """
    common = (
        portfolio_returns.index
        .intersection(benchmark_returns.index)
        .intersection(portfolio_weights.index)
        .intersection(benchmark_weights.index)
    )
    if len(common) == 0:
        return {"allocation": 0.0, "selection": 0.0, "interaction": 0.0, "total": 0.0}

    Rp = portfolio_returns.loc[common]
    Rb = benchmark_returns.loc[common]
    Wp = portfolio_weights.loc[common]
    Wb = benchmark_weights.loc[common]

    # Allocation: (Wp - Wb) * Rb
    allocation = ((Wp - Wb) * Rb).sum()
    # Selection: Wb * (Rp - Rb)
    selection = (Wb * (Rp - Rb)).sum()
    # Interaction: (Wp - Wb) * (Rp - Rb)
    interaction = ((Wp - Wb) * (Rp - Rb)).sum()

    total = allocation + selection + interaction
    logger.debug(f"[brinson] allocation={allocation:.4f} selection={selection:.4f} interaction={interaction:.4f} total={total:.4f}")

    return {
        "allocation": round(allocation, 6),
        "selection": round(selection, 6),
        "interaction": round(interaction, 6),
        "total": round(total, 6),
    }


def factor_exposure_decomposition(
    portfolio_returns: pd.Series,
    factor_returns: pd.DataFrame,
) -> pd.Series:
    """因子暴露分解: 用时间序列回归估计组合在各因子上的暴露。

    portfolio_returns: 组合日收益序列
    factor_returns: index=date, columns=factor_name 的因子收益

    返回: Series(index=factor_name, value=beta)
    """
    common_dates = portfolio_returns.index.intersection(factor_returns.index)
    if len(common_dates) < 20:
        return pd.Series(dtype=float)

    y = portfolio_returns.loc[common_dates].values
    X = factor_returns.loc[common_dates].values

    # OLS: y = X @ beta + epsilon
    try:
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
    except np.linalg.LinAlgError:
        return pd.Series(0.0, index=factor_returns.columns)

    return pd.Series(beta, index=factor_returns.columns)


def compute_sharpe(returns: pd.Series, rf: float = None, periods: int = None) -> float:
    """年化 Sharpe ratio。

    rf:      无风险利率 (默认从 config attribution.risk_free_rate 读取)
    periods: 年化天数 (默认从 config attribution.annual_periods 读取)
    """
    if rf is None:
        rf = _RF_RATE
    if periods is None:
        periods = _ANNUAL_PERIODS
    er = returns.mean() * periods - rf
    std = returns.std() * np.sqrt(periods)
    return er / std if std > 0 else 0.0


def compute_max_drawdown(returns: pd.Series) -> float:
    """最大回撤 (从峰值到谷底的最大跌幅)。"""
    cumulative = (1 + returns).cumprod()
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max
    return float(drawdown.min())


def compute_win_rate(trades: list[dict]) -> float:
    """胜率: 盈利交易数 / 总交易数。"""
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    return wins / len(trades)
