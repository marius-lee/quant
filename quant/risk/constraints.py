"""风险约束 — 单票仓位上限、行业暴露上限、流动性门槛、ST 过滤。

风控层不对 alpha 加分，只做减法和约束。任何不满足约束的股票被移除候选池。

所有默认值均从 config/config.yaml 读取（单一真相源）。代码中的 fallback 默认值仅在
配置文件缺失对应 key 时生效，不应作为正常运作的默认参数使用。
"""

import pandas as pd
from typing import Optional
from dataclasses import dataclass

from quant.config.constants import _require_cfg


@dataclass
class RiskLimits:
    """风险约束参数集。

    所有默认值来源: config/config.yaml → risk.*（单一真相源）。
    调用方可通过 from_config() 构造，或直接传参覆盖。
    """
    # 单票最大仓位 5%（行业标准: 5-10%, 来源: Grinold & Kahn 1999, config risk.max_single_position）
    max_single_position: float = 0.05
    # 最大持仓数（Grinold & Kahn: 20-50只, config risk.max_positions）
    max_positions: int = 20
    # 最低日成交额(元), A股<500万/日无法有效进出（来源: 实际盘口观测, config risk.min_daily_amount）
    min_daily_amount: float = 500_000
    # 单行业最大暴露 40%（行业惯例, BARRA 风险模型, config risk.max_sector_exposure）
    max_sector_exposure: float = 0.40
    # 排除 *ST/ST 股票（A股退市风险, config risk.exclude_star_st）
    exclude_star_st: bool = True
    # 最低股价(元), 低于此值过滤仙股（A股面值1元, <2元视为退市风险, config risk.min_price）
    min_price: float = 2.0

    @classmethod
    def from_config(cls) -> "RiskLimits":
        """从 config/config.yaml 读取风险约束参数（单一真相源）。

        config.yaml 中每个参数都有完整的文献/业界来源注释。
        此方法确保代码中不会出现与配置文件不一致的硬编码默认值。
        类属性默认值仅作为配置文件缺失 key 时的最后 fallback。
        """
        return cls(
            max_single_position=_require_cfg("risk.max_single_position"),
            max_positions=_require_cfg("risk.max_positions"),
            min_daily_amount=_require_cfg("risk.min_daily_amount"),
            max_sector_exposure=_require_cfg("risk.max_sector_exposure"),
            exclude_star_st=_require_cfg("risk.exclude_star_st"),
            min_price=_require_cfg("risk.min_price"),
        )


def filter_by_liquidity(
    candidates: pd.DataFrame,
    min_daily_amount: float,
) -> pd.DataFrame:
    """流动性过滤: 去掉日均成交额过低的股票。

    candidates: DataFrame, index=symbol, 至少含 amount 列 (千元)
    min_daily_amount: 最低日均成交额 (元),
                      来源 config risk.min_daily_amount (500 万, A 股中小盘盘口流动性差)

    返回: 满足流动性要求的 subset。

    来源: 实际盘口观测 — A股中小盘盘口流动性差, <500万/日几乎无法以合理价格成交
    """
    # amount 在数据库中单位为千元, 转换为元
    daily_amount_yuan = candidates["amount"] * 1000
    valid = daily_amount_yuan >= min_daily_amount
    return candidates[valid].copy()


def filter_by_price(
    candidates: pd.DataFrame,
    min_price: float,
) -> pd.DataFrame:
    """低价股过滤: 去掉股价过低的仙股（流动性差、容易退市）。

    min_price 阈值来源: config risk.min_price。
    A 股面值 1 元, <2 元视为仙股高风险（退市风险警示板块）。
    实际参数取值由 RiskLimits.from_config() 从 config.yaml 读取。
    """
    valid = candidates["close"] >= min_price
    return candidates[valid].copy()


def filter_st_stocks(
    candidates: pd.DataFrame,
    stock_names: Optional[dict] = None,
) -> pd.DataFrame:
    """ST 股过滤: 移除名称含 *ST 或 ST 的股票。

    stock_names: {symbol: name} 名称映射 (从 DataStore.get_stock_names() 获取)

    来源: A 股退市规则 — ST/*ST 股票涨跌幅限制 5% 且存在退市风险，
    不适合量化策略（exclude_star_st: true in config risk）。
    """
    if stock_names is None:
        return candidates
    is_st = pd.Series(False, index=candidates.index)
    for sym in candidates.index:
        name = stock_names.get(sym, "")
        # 安全处理: stock_names 中可能存在非字符串值
        if name and "ST" in str(name).upper():
            is_st[sym] = True
    return candidates[~is_st].copy()


def filter_sealed_limit_up(candidates, prev_date: str, seal_ratio_threshold: float = 3.0):
    """Exclude stocks sealed at limit-up on previous trading day.

    Sources: ADR-033 limit order design + test-v210 exec feedback loop.
    limit_up_pool table in market.db, synced daily by daily_sync.py.
    seal_ratio = lock_capital / amount; higher = stronger seal.
    """
    import sqlite3
    from quant.config.paths import MARKET_DB
    conn = sqlite3.connect(MARKET_DB)
    try:
        rows = conn.execute(
            "SELECT symbol, lock_capital, amount FROM limit_up_pool WHERE date=?",
            (prev_date,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return candidates.copy()
    sealed_syms = set()
    for sym, lock_cap, amt in rows:
        try:
            lc = float(lock_cap or 0)
            a = float(amt or 0)
            if lc > 0 and a > 0 and (lc / a) > seal_ratio_threshold:
                sealed_syms.add(sym)
        except (ValueError, TypeError):
            pass
    from quant.utils.logger import get_logger
    logger = get_logger("risk.constraints")
    removed = [s for s in sealed_syms if s in candidates.index]
    if removed:
        logger.info(f"limit-up filter: {prev_date} sealed={len(sealed_syms)} removed={len(removed)}")
    return candidates[~candidates.index.isin(sealed_syms)].copy()



def apply_all_filters(
    candidates: pd.DataFrame,
    limits: Optional[RiskLimits] = None,
    stock_names: Optional[dict] = None,
    industries: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """应用所有风险过滤。

    过滤顺序: 流动性 → 股价 → ST (行业暴露上限由调用方单独检查, 2026-07-21 audit M10)

    当 limits 为 None 时，自动从 config/config.yaml 读取默认风险参数。
    这确保了代码中没有与配置文件不一致的硬编码数值。

    返回: 通过所有约束的候选池 DataFrame。
    """
    # ── BJ 过滤说明 (P3) ──
    # BJ(北交所 92xxxx/4xxxxx/8xxxxx) 已在 pipeline Step 2 SQL 层面通过
    # WHERE s.market!='BJ' 排除，此处不重复过滤。若直接调用 apply_all_filters()
    # 且含 BJ 股票，调用方自行预过滤。BJ 涨跌停±30% 且需50万保证金。

    if limits is None:
        # 从 config.yaml 读取默认风险参数（单一真相源）。
        # RiskLimits.from_config() 会逐项读取 config 文件中的值，
        # 缺失时回退到类属性默认值（与 config.yaml 保持一致）。
        limits = RiskLimits.from_config()

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
    from quant.utils.logger import get_logger
    logger = get_logger("risk.constraints")
    logger.info(f"risk filters: {n_before} → {n_after} (removed {n_before - n_after})")
    return df


def position_limit_check(
    weights: pd.Series,
    max_single: float,
    max_positions: int,
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
    max_exposure: float,
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
