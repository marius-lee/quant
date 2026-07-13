"""风险中性化 — 截面回归取残差，消除行业和市值 bias。

方法:
  industry_neutralize  — 行业内排名 → 消除行业 beta
  size_neutralize      — 截面回归 alpha ~ log(market_cap) → 残差 = 纯选股 alpha

来源:
  ② Grinold & Kahn (2000) Chapter 4 — 风险模型中的因子暴露
  ② Fama & French (1993) — 三因子模型中的市值效应
  ② BARRA 风险模型 — 行业中性化标准做法
"""

from utils.logger import get_logger
logger = get_logger("risk.neutralize")

import numpy as np

from config.constants import _require_cfg
_MIN_COMMON = _require_cfg("risk.neutralize.min_common_stocks")
import pandas as pd
from scipy import stats
from typing import Optional


def industry_neutralize(
    scores: pd.Series,
    industries: pd.Series,
    min_stocks_per_industry: int = 3,
) -> pd.Series:
    """行业中性化: 每个行业内做 z-score 标准化。

    消除行业 beta 的影响 — 高分不再来自「选对了行业」，而是「选对了行业内个股」。

    scores: index=symbol, alpha 得分
    industries: index=symbol, 行业分类 (e.g. "银行", "医药")
    min_stocks_per_industry: 行业内最少股票数 (低于此值不做中性化)

    返回: 行业中性化后的得分 Series，整体再做 z-score。

    来源: ② BARRA USE4 — 行业因子中性化
    """
    aligned = scores.dropna()
    ind_aligned = industries.reindex(aligned.index).dropna()
    common = aligned.index.intersection(ind_aligned.index)

    if len(common) < _MIN_COMMON:
        return scores

    neutralized = pd.Series(np.nan, index=scores.index)

    for industry, group in ind_aligned.groupby(ind_aligned):
        syms = group.index.intersection(common)
        if len(syms) < min_stocks_per_industry:
            continue
        # 行业内 z-score
        vals = scores.loc[syms]
        z = (vals - vals.mean()) / vals.std(ddof=1)
        neutralized.loc[syms] = z

    # 整体再标准化
    valid = neutralized.dropna()
    if len(valid) < _MIN_COMMON:
        return scores

    result = (valid - valid.mean()) / valid.std(ddof=1)
    return result.reindex(scores.index)


def size_neutralize(
    scores: pd.Series,
    market_caps: pd.Series,
) -> pd.Series:
    """市值中性化: 截面回归 alpha ~ log(market_cap)，取残差。

    消除市值效应: A 股小市值溢价显著，中性化后 alpha 反映的是「纯选股能力」，
    而非「买了小盘股」。

    scores: index=symbol, alpha 得分
    market_caps: index=symbol, 总市值 (元)

    返回: 残差 = 纯选股 alpha (去均值标准化)

    来源: ② Fama & French (1993) — 市值因子 (SMB)
    """
    common = scores.dropna().index.intersection(market_caps.dropna().index)

    if len(common) < _MIN_COMMON:
        return scores

    y = scores.loc[common].values
    X = np.log(market_caps.loc[common].values)

    # OLS: y = α + β × log(mcap) + ε
    X_with_const = np.column_stack([np.ones(len(X)), X])
    beta = np.linalg.lstsq(X_with_const, y, rcond=None)[0]
    y_pred = X_with_const @ beta
    residuals = y - y_pred

    result = pd.Series(residuals, index=common)
    result = (result - result.mean()) / result.std(ddof=1)
    return result.reindex(scores.index)


def neutralize(
    scores: pd.Series,
    industries: Optional[pd.Series] = None,
    market_caps: Optional[pd.Series] = None,
) -> pd.Series:
    """统一的 alpha 中性化入口。

    顺序: 行业中性化 → 市值中性化（两者都提供时）

    scores: index=symbol, alpha 得分
    industries: 行业分类 (可选)
    market_caps: 总市值 (可选)

    返回: 中性化后的得分
    """
    result = scores.copy()
    ind_flag = "Y" if industries is not None else "N"
    sz_flag = "Y" if market_caps is not None else "N"
    logger.info(f"[neutralize] industry={ind_flag} size={sz_flag}")

    if industries is not None:
        result = industry_neutralize(result, industries)

    if market_caps is not None:
        result = size_neutralize(result, market_caps)

    return result
