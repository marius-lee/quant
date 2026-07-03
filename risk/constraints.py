"""风险约束 — 单票仓位上限、行业暴露上限、流动性门槛、ST 过滤。

风控层不对 alpha 加分，只做减法和约束。任何不满足约束的股票被移除候选池。
"""

import pandas as pd
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class RiskLimits:
    """风险约束参数集。

    max_single_position: 单票最大仓位 (占组合比例)
    max_positions:      最大持仓数
    min_daily_amount:   最低日成交额 (元), 低于此值的股票无法买卖 (流动性门槛)
    max_sector_exposure: 最大行业暴露 (占组合比例), 0 = 不限制
    exclude_star_st:    排除 *ST / ST 股票
    min_price:          最低股价 (元), 低于此值过滤 (仙股风险)
    """
    max_single_position: float = 0.20
    max_positions: int = 20
    min_daily_amount: float = 5_000_000  # 500万, A股日成交<此值无法有效进出
    max_sector_exposure: float = 0.40    # 单行业最多40%
    exclude_star_st: bool = True
    min_price: float = 2.0               # 删除低于2元的仙股


def filter_by_liquidity(
    candidates: pd.DataFrame,
    min_daily_amount: float = 5_000_000,
) -> pd.DataFrame:
    """流动性过滤: 去掉日均成交额过低的股票。

    candidates: DataFrame, index=symbol, 至少含 amount 列 (千元)
    min_daily_amount: 最低日均成交额 (元)

    返回: 满足流动性要求的 subset。

    来源: ③ 数据校准 — A股中小盘盘口流动性差, <500万/日几乎无法以合理价格成交
    """
    # amount 在数据库中单位为千元, 转换为元
    daily_amount_yuan = candidates["amount"] * 1000
    valid = daily_amount_yuan >= min_daily_amount
    return candidates[valid].copy()


def filter_by_price(
    candidates: pd.DataFrame,
    min_price: float = 2.0,
) -> pd.DataFrame:
    """低价股过滤: 去掉股价过低的仙股（流动性差、容易退市）。"""
    valid = candidates["close"] >= min_price
    return candidates[valid].copy()


def filter_st_stocks(
    candidates: pd.DataFrame,
    stock_names: Optional[dict] = None,
) -> pd.DataFrame:
    """ST 股过滤: 移除名称含 *ST 或 ST 的股票。

    stock_names: {symbol: name} 名称映射 (从 DataStore.get_stock_names() 获取)
    """
    if stock_names is None:
        return candidates
    is_st = pd.Series(False, index=candidates.index)
    for sym in candidates.index:
        name = stock_names.get(sym, "")
        if "ST" in name.upper():
            is_st[sym] = True
    return candidates[~is_st].copy()


def apply_all_filters(
    candidates: pd.DataFrame,
    limits: Optional[RiskLimits] = None,
    stock_names: Optional[dict] = None,
    industries: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """应用所有风险过滤。

    过滤顺序: 流动性 → 股价 → ST → 行业暴露上限

    返回: 通过所有约束的候选池 DataFrame。
    """
    # ── BJ 过滤说明 (P3) ──
    # BJ(北交所 92xxxx/4xxxxx/8xxxxx) 已在 pipeline Step 2 SQL 层面通过
    # WHERE s.market!='BJ' 排除，此处不重复过滤。若直接调用 apply_all_filters()
    # 且含 BJ 股票，调用方自行预过滤。BJ 涨跌停±30% 且需50万保证金。
    if limits is None:
        limits = RiskLimits()
    df = candidates.copy()
    n_before = len(df)
    # 1. 流动性
    if "amount" in df.columns:
        df = filter_by_liquidity(df, limits.min_daily_amount)
    # 2. 股价
    if "close" in df.columns:
        df = filter_by_price(df, limits.min_price)
    # 3. ST
    if limits.exclude_star_st:
        df = filter_st_stocks(df, stock_names)
    n_after = len(df)
    from utils.logger import get_logger
    logger = get_logger("risk.constraints")
    logger.info(f"risk filters: {n_before} → {n_after} (removed {n_before - n_after})")
    return df


def position_limit_check(
    weights: pd.Series,
    max_single: float = 0.20,
    max_positions: int = 20,
) -> tuple[bool, str]:
    """检查持仓是否违反约束。

    返回: (is_valid, message)
    """
    if len(weights) > max_positions:
        return False, f"positions count {len(weights)} > max {max_positions}"
    over = weights[weights > max_single]
    if len(over) > 0:
        return False, f"position limit exceeded: {over.index.tolist()} at {over.values}"
    return True, "OK"


def sector_exposure_check(
    weights: pd.Series,
    industries: pd.Series,
    max_exposure: float = 0.40,
) -> tuple[bool, str]:
    """检查行业暴露是否超过上限。

    返回: (is_valid, message)
    """
    if max_exposure <= 0:
        return True, "no sector limit"
    # 按行业汇总权重
    common = weights.index.intersection(industries.index)
    sector_weights = pd.Series(weights.loc[common].values,
                               index=industries.loc[common].values)
    exposures = sector_weights.groupby(sector_weights.index).sum()
    over = exposures[exposures > max_exposure]
    if len(over) > 0:
        return False, f"sector exposure exceeded: {over.to_dict()}"
    return True, "OK"
