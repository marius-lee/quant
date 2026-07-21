"""Kelly 公式头寸管理 — 替代等权分配，按因子预期收益分配仓位。

⚠️  ADR 032: 此模块仅在 Small 层 (≥¥100K) 的 construct() 中被调用。
Nano/Micro 层不使用 Kelly — 整数手离散化误差压倒优化收益。
严禁在低资本层引入 Kelly，这是已验证的反复出现的错误。

理论来源: Kelly (1956) "A New Interpretation of Information Rate".
Fractional Kelly: 使用 1/N 凯利降低波动 (Ralph Vince 1990).

集成点: 在 optimizer/portfolio.py 的 _equal_weight_greedy 之前调用，
用 Kelly 分数替代等权分配。

公式:
  Kelly 比例 = (μ - r_f) / σ²
  μ  = 因子预期收益 (IC × 日波动率中位数)
  σ² = 因子收益方差
  Fractional Kelly: f* = Kelly / k (k=4 为四分之一凯利, 保守)
"""

import numpy as np
import pandas as pd
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

_log = get_logger("optimizer.kelly")


def compute_kelly_fractions(
    alpha: pd.Series,
    ic_map: dict = None,
    fraction: float = None,
) -> pd.Series:
    """计算每只候选股的 Kelly 分数.

    Args:
        alpha: 股票得分 Series (index=symbol, value=score)
        ic_map: {factor_name: ic_value} — 各因子的 IC 值, 用于估算预期收益.
                如果为 None 或空, 按 alpha 得分比例分配 (退化到等权).
        fraction: Fractional Kelly 分数, 默认从 config 读 (optimizer.kelly_fraction).

    Returns:
        Series (index=symbol, value=Kelly fraction) — 总和 ≤ 1.
    """
    if fraction is None:
        fraction = _require_cfg("optimizer.kelly_fraction")

    # 展平嵌套 ic_map (兼容 compute_ic 产出格式)
    if ic_map and isinstance(next(iter(ic_map.values())), dict):
        ic_map = {k: v.get("ic_ir", 0) for k, v in ic_map.items()}
        _log.debug("Flattened nested ic_map -> %d factors", len(ic_map))

    if ic_map is None or not ic_map:
        # 无 IC 信息 → 退化为 alpha 比例分配
        _log.debug("No IC map — falling back to alpha-proportional allocation")
        return _alpha_proportional(alpha)

    # ── 从 IC 估算预期收益 ──
    # 每个股票的预期收益 = 各因子 IC 加权 × 股票在该因子的得分
    # 简化: 用 med_IC 作为全局预期收益参数
    ic_values = np.array(list(ic_map.values()))
    med_ic = np.median(np.abs(ic_values))

    # IC 退化保护: 所有 IC=0 → 退化为 alpha 比例
    if med_ic < 1e-6:
        _log.debug("All IC ≈ 0 — falling back to alpha-proportional")
        return _alpha_proportional(alpha)

    # 预期收益 μ = med_IC × 日波动率代理
    # 使用 alpha 得分的标准化值作为 μ 的代理
    mu = alpha / alpha.abs().max() * med_ic if alpha.abs().max() > 0 else alpha

    # σ²: A股日收益率典型方差 ≈ 0.0004 (σ_daily ≈ 2%)
    # 来源: CSRC 2025年度报告 + 2026-07-21 audit C5
    # alpha.var() 是截面方差(~1.0), 非收益率方差, 会导致 Kelly ~0
    DEFAULT_RETURN_VAR = 0.0004
    var = DEFAULT_RETURN_VAR

    # Kelly: f = (μ - r_f) / σ², r_f=0 (A股无风险利率极低)
    kelly_raw = mu / max(var, 1e-8)

    # 过滤负 Kelly (因子预期该股下跌)
    kelly_raw = kelly_raw.clip(lower=0)

    # Fractional Kelly: 除以 k
    kelly = kelly_raw / fraction

    # ── 归一化: 总和不超过 1 ──
    total = kelly.sum()
    if total > 0:
        kelly = kelly / total
    else:
        return _alpha_proportional(alpha)

    # 单只股票上限
    max_single = _require_cfg("risk.max_single_position")
    kelly = kelly.clip(upper=max_single)
    if kelly.sum() > 0:
        kelly = kelly / kelly.sum()

    _log.debug(
        f"Kelly fractions: {len(kelly)} stocks, "
        f"top3={kelly.iloc[:3].round(3).to_dict() if len(kelly) >= 3 else kelly.round(3).to_dict()}"
    )
    return kelly


def _alpha_proportional(alpha: pd.Series) -> pd.Series:
    """退化为 alpha 得分比例分配 (等权的变体)."""
    if alpha.sum() == 0:
        return pd.Series(1.0 / max(len(alpha), 1), index=alpha.index)
    return alpha / alpha.sum()


def compute_lot_allocation(
    alpha: pd.Series,
    prices: pd.Series,
    capital: float,
    ic_map: dict = None,
    max_positions: int = None,
    lot_size: int = 100,
) -> tuple[pd.Series, float]:
    """用 Kelly 分数计算整数手分配.

    Args:
        alpha: 股票得分 (index=symbol, value=score)
        prices: 股价 (index=symbol, value=price)
        capital: 可用资金
        ic_map: 因子 IC 映射
        max_positions: 最大持仓数
        lot_size: 每手股数 (A股=100)

    Returns:
        (lots Series, remaining_cash)
    """
    if max_positions is None:
        max_positions = _require_cfg("risk.max_positions")

    n = min(max_positions, len(alpha))
    top_alpha = alpha.iloc[:n]
    top_prices = prices.loc[top_alpha.index]

    kelly_weights = compute_kelly_fractions(top_alpha, ic_map)
    kelly_weights = kelly_weights.loc[top_alpha.index].fillna(0)
    if kelly_weights.sum() == 0:
        kelly_weights = pd.Series(1.0 / n, index=top_alpha.index)

    lots = pd.Series(0, index=top_alpha.index, dtype=int)
    cash = capital

    # 按 Kelly 权重分配资金
    for sym in top_alpha.index:
        alloc = capital * kelly_weights.get(sym, 0)
        n_lots = int(alloc / (top_prices[sym] * lot_size))
        if n_lots > 0:
            cost = n_lots * top_prices[sym] * lot_size
            if cost <= cash:
                lots[sym] = n_lots
                cash -= cost

    # 残差分配: 把剩余现金按最便宜能买的股票分配
    if cash > 0 and lots.sum() == 0:
        # 全部股票都买不起 → 买最便宜的 1 手
        cheapest_sym = top_prices.idxmin()
        cheapest_cost = top_prices[cheapest_sym] * lot_size
        if cash >= cheapest_cost:
            lots[cheapest_sym] = 1
            cash -= cheapest_cost

    return lots[lots > 0], round(cash, 2)
