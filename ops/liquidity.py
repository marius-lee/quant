"""市场微观结构 — 流动性指标 & 波动性分解.
来源: Larry Harris《交易与交易所》第20章

Roll序列协方差价差估计: S = 2 × √(-Cov(ΔP_t, ΔP_{t-1}))
  假设: 基本面随机游走, 买卖等概率, 交易不含信息
  用法: 仅在没有买卖报价时使用 (Harris原文)

波动性分解: Var(ΔP) = σ²_ε(基本) + S²/2(临时)
  基本波动: 不可逆, 信息驱动
  临时波动: 可逆, 非知情交易驱动
"""
import sqlite3, os, math

MARKET_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")


def roll_spread(symbol: str, mc: sqlite3.Connection = None,
                window: int = 40) -> dict:
    """Roll序列协方差价差估计.
    S = 2 × √(-Cov(ΔP_t, ΔP_{t-1}))
    来源: Harris 第20章, 行107605-107776

    Args:
        symbol: 股票代码
        mc: market.db连接(可选, 不传则自建)
        window: 估计窗口(交易日, 默认40)

    Returns:
        spread_absolute: 绝对价差S (元)
        spread_relative: 相对价差 S/均价 (%)
        covariance: 序列协方差值
        valid: 模型是否适用
    """
    close_db = mc is None
    if close_db:
        mc = sqlite3.connect(MARKET_DB)

    rows = mc.execute("""
        SELECT close FROM daily
        WHERE symbol=? AND close > 0
        ORDER BY date DESC LIMIT ?
    """, (symbol, window + 1)).fetchall()

    if close_db:
        mc.close()

    if len(rows) < window:
        return {"valid": False, "reason": f"数据不足: 需要{window}天, 仅有{len(rows)}天"}

    # 时间顺序: rows是DESC, 需要反转
    prices = [r[0] for r in reversed(rows)]
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    n = len(deltas)

    mean_d = sum(deltas) / n
    # Cov(ΔP_t, ΔP_{t-1}) 使用1阶滞后
    cov = sum((deltas[t] - mean_d) * (deltas[t-1] - mean_d)
              for t in range(1, n)) / (n - 2)

    if cov >= 0:
        return {"valid": False, "reason": f"Cov≥0 ({cov:.6f}), 无负序列相关, 模型不适用"}

    S = 2 * math.sqrt(-cov)
    mean_price = sum(prices) / len(prices)
    rel_spread = S / mean_price * 100 if mean_price > 0 else 0

    return {
        "valid": True,
        "spread_absolute": round(S, 4),
        "spread_relative": round(rel_spread, 4),
        "covariance": round(cov, 6),
        "n": n
    }


def volatility_decompose(symbol: str, mc: sqlite3.Connection = None,
                          window: int = 40) -> dict:
    """波动性成分分解.
    Var(ΔP) = σ²_ε(基本面方差) + S²/2(临时方差)
    来源: Harris 第20章, 行107524-107604

    Returns:
        total_var: 总方差 Var(ΔP)
        fundamental_var: 基本面方差 (永久, 信息驱动)
        transitory_var: 临时方差 (可逆, 非知情交易驱动)
        transitory_ratio: 临时波动占比 (0~1)
        valid: 是否有效
    """
    spread = roll_spread(symbol, mc, window)
    if not spread["valid"]:
        return {"valid": False, "reason": spread.get("reason", "价差估计失败")}

    close_db = mc is None
    if close_db:
        mc = sqlite3.connect(MARKET_DB)

    rows = mc.execute("""
        SELECT close FROM daily
        WHERE symbol=? AND close > 0
        ORDER BY date DESC LIMIT ?
    """, (symbol, window + 1)).fetchall()

    if close_db:
        mc.close()

    prices = [r[0] for r in reversed(rows)]
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    n = len(deltas)

    mean_d = sum(deltas) / n
    total_var = sum((d - mean_d)**2 for d in deltas) / (n - 1)

    S = spread["spread_absolute"]
    transitory_var = S * S / 2.0

    # 基本面方差 = 总方差 - 临时方差 (来源: Harris行107636-107637)
    fundamental_var = total_var - transitory_var
    if fundamental_var < 0:
        # 临时方差估计过大 → 总方差几乎全为临时
        fundamental_var = 0
        transitory_var = total_var

    ratio = transitory_var / total_var if total_var > 0 else 0

    return {
        "valid": True,
        "total_var": round(total_var, 6),
        "fundamental_var": round(fundamental_var, 6),
        "transitory_var": round(transitory_var, 6),
        "transitory_ratio": round(ratio, 4),
        "spread_relative": spread["spread_relative"]
    }
