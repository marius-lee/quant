"""性能计算 — Alpha转换, 成本常量, Kelly分数。"""

# 交易成本 (来源: A股税法 + 行业标准)
BUY_COST = 0.0003    # 万三佣金
SELL_COST = 0.0013   # 千一印花税 + 万三佣金

# Grinold 默认值
RESIDUAL_VOL_DEFAULT = 0.02   # 残差波动率 2%
IC_PRIOR = 0.05               # 先验 IC


def alpha_from_score(score: float, mode: str = "chen") -> float:
    """Z-score → Alpha 预期收益。来源: Grinold — Alpha = Vol × IC × Score。"""
    return RESIDUAL_VOL_DEFAULT * IC_PRIOR * (score - 0.5) * 2


def kelly_fraction(strategy: str = "chen", n_positions: int = 1, drawdown_pct: float = 0.0) -> float:
    """Kelly 仓位比例。来源: Chan 半Kelly, 多仓位折扣, 回撤缩放。"""
    base = 0.5  # 半 Kelly (来源: Chan — 半Kelly比全Kelly更稳健)
    # 多仓位折扣: 每多一个仓位降低集中度
    discount = max(0.25, 1.0 / max(n_positions, 1))
    # 回撤缩放: 回撤越大仓位越小
    dd_scale = max(0.25, 1.0 - abs(drawdown_pct) * 10)
    return base * discount * dd_scale


def mcva_trailing_stop(entry_price: float, peak_price: float, current_price: float,
                       vol: float = RESIDUAL_VOL_DEFAULT) -> bool:
    """MCVA 移动止盈触发。来源: Harris 波动分解。"""
    if peak_price <= entry_price:
        return False
    drawdown = (peak_price - current_price) / peak_price
    return drawdown > vol * 2.5
