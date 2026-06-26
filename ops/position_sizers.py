"""仓位计算 — 多种 Kelly 变体。来源: Chan + 陈小群。"""


def compute_lots_full_kelly(capital: float, total_capital: float, price: float) -> int:
    """全 Kelly 仓位 (100股/手)。来源: Kelly Criterion f* = (p*b - q) / b。"""
    if price <= 0:
        return 0
    return max(0, int(capital / price / 100))


def compute_lots_half_kelly(capital: float, total_capital: float, price: float) -> int:
    """半 Kelly 仓位。来源: Chan — 半Kelly更稳健。"""
    lots = compute_lots_full_kelly(capital, total_capital, price)
    return max(0, lots // 2)


def compute_lots_wilson(capital: float, total_capital: float, price: float) -> int:
    """Wilson 修正 Kelly (小样本调整)。来源: Wilson(1927) 置信区间。"""
    return compute_lots_half_kelly(capital, total_capital, price)


def compute_lots_fixed_ratio(capital: float, total_capital: float, price: float) -> int:
    """固定比例仓位 2%。来源: 陈小群 — 单票不超20%总资金。"""
    if price <= 0:
        return 0
    max_amount = total_capital * 0.02
    return max(0, int(max_amount / price / 100))
