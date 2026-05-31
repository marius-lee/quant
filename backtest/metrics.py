"""回测绩效指标。

计算: 年化收益、波动率、夏普比率、最大回撤、Calmar比率、胜率等。
"""

import numpy as np
import pandas as pd
from utils.logger import get_logger

logger = get_logger("backtest.metrics")


TRADING_DAYS = 252


def compute_metrics(
    returns: pd.Series,
    benchmark_returns: pd.Series = None,
    initial_capital: float = 1_000_000.0,
) -> dict:
    """计算完整的回测绩效指标"""
    returns = returns.dropna()
    if len(returns) == 0:
        return {}

    cumulative = (1 + returns).cumprod()

    # 年化收益率
    total_return = cumulative.iloc[-1] - 1
    years = len(returns) / TRADING_DAYS
    if years < 0.25:
        logger.warning(f"metrics: only {len(returns)} trading days ({years:.3f} years), "
                       f"annualization may be unreliable")
    annual_return = (1 + total_return) ** (1 / max(years, 0.25)) - 1

    # 年化波动率
    annual_vol = returns.std() * np.sqrt(TRADING_DAYS)

    # 夏普比率（假设无风险利率 2%）
    risk_free = 0.02
    sharpe = (annual_return - risk_free) / annual_vol if annual_vol > 0 else 0

    # 最大回撤
    peak = cumulative.expanding().max()
    drawdown = (cumulative - peak) / peak
    max_drawdown = drawdown.min()

    # Calmar 比率
    calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0

    # 胜率
    win_rate = (returns > 0).mean()

    # 盈亏比
    avg_win = returns[returns > 0].mean() if (returns > 0).any() else 0
    avg_loss = abs(returns[returns < 0].mean()) if (returns < 0).any() else 0
    profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")

    # 超额收益（相对基准）
    alpha = 0
    beta = 0
    information_ratio = 0
    if benchmark_returns is not None:
        benchmark_returns = benchmark_returns.loc[returns.index]
        common = returns.index.intersection(benchmark_returns.index)
        if len(common) > 60:
            r = returns.loc[common]
            b = benchmark_returns.loc[common]
            excess = r - b
            # Jensen's Alpha: (r_p - r_f) - beta * (r_b - r_f)
            rf_daily = 0.02 / TRADING_DAYS
            beta = np.cov(r, b)[0, 1] / np.var(b) if np.var(b) > 0 else 0
            alpha = (r.mean() - rf_daily - beta * (b.mean() - rf_daily)) * TRADING_DAYS
            tracking_error = excess.std() * np.sqrt(TRADING_DAYS)
            information_ratio = (
                excess.mean() * TRADING_DAYS / tracking_error
                if tracking_error > 0 else 0
            )

    # 最终资金
    final_value = initial_capital * cumulative.iloc[-1]

    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_volatility": annual_vol,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_drawdown,
        "calmar_ratio": calmar,
        "win_rate": win_rate,
        "profit_loss_ratio": profit_loss_ratio,
        "alpha": alpha,
        "beta": beta,
        "information_ratio": information_ratio,
        "final_value": final_value,
        "total_days": len(returns),
    }


def print_metrics(metrics: dict):
    """格式化打印指标"""
    print("=" * 50)
    print(f"{'回测绩效报告':^50}")
    print("=" * 50)
    print(f"  总收益率:     {metrics.get('total_return', 0):>8.2%}")
    print(f"  年化收益率:   {metrics.get('annual_return', 0):>8.2%}")
    print(f"  年化波动率:   {metrics.get('annual_volatility', 0):>8.2%}")
    print(f"  夏普比率:     {metrics.get('sharpe_ratio', 0):>8.2f}")
    print(f"  最大回撤:     {metrics.get('max_drawdown', 0):>8.2%}")
    print(f"  Calmar比率:   {metrics.get('calmar_ratio', 0):>8.2f}")
    print(f"  胜率:         {metrics.get('win_rate', 0):>8.2%}")
    print(f"  盈亏比:       {metrics.get('profit_loss_ratio', 0):>8.2f}")
    print(f"  Alpha:        {metrics.get('alpha', 0):>8.4f}")
    print(f"  Beta:         {metrics.get('beta', 0):>8.2f}")
    print(f"  信息比率:     {metrics.get('information_ratio', 0):>8.2f}")
    print(f"  最终资金:     {metrics.get('final_value', 0):>12,.0f}")
    print("=" * 50)
