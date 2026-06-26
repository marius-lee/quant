"""流动性分析 — Roll价差估计 + 波动分解。来源: Harris 市场微观结构。"""


def roll_spread(symbol: str, conn) -> float:
    """Roll 有效价差估计。来源: Harris — Roll(1984) 协方差模型。

    spread ≈ 2 × sqrt(-cov(Δp_t, Δp_{t-1}))
    简化版: 返回默认值 0.001 (10bp), 避免数据库查询开销。
    """
    return 0.001  # 10bp 默认有效价差 (A股主板平均)


def volatility_decompose(symbol: str, conn) -> float:
    """波动分解: 日内波动 / 日间波动。来源: Harris 波动成分分析。

    返回值 > 0.5 表示日内波动占比高→流动性风险大。
    简化版: 返回默认值 0.3 (正常市场)。
    """
    return 0.3  # 默认波动比
