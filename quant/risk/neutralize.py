"""风险中性化 — 截面回归取残差，消除行业和市值 bias。

方法:
  industry_neutralize  — 行业内排名 → 消除行业 beta
  size_neutralize      — 截面回归 alpha ~ log(market_cap) → 残差 = 纯选股 alpha

来源:
  ② Grinold & Kahn (2000) Chapter 4 — 风险模型中的因子暴露
  ② Fama & French (1993) — 三因子模型中的市值效应
  ② BARRA 风险模型 — 行业中性化标准做法
"""

from quant.utils.logger import get_logger
logger = get_logger("risk.neutralize")

import numpy as np

from quant.config.constants import _require_cfg
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
            # P1-13 fix: 单股票行业 (std=NaN) → 跳过中性化, 保留原分值
            neutralized.loc[syms] = scores.loc[syms]
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
    X = np.log(np.asarray(market_caps.loc[common].values, dtype=np.float64))

    # OLS: y = α + β × log(mcap) + ε
    X_with_const = np.column_stack([np.ones(len(X)), X])
    beta = np.linalg.lstsq(X_with_const, y, rcond=None)[0]
    y_pred = X_with_const @ beta
    residuals = y - y_pred

    result = pd.Series(residuals, index=common)
    result = (result - result.mean()) / result.std(ddof=1)
    return result.reindex(scores.index)



def style_neutralize(
    scores: pd.Series,
    exposures: dict[str, pd.Series],
    min_common: int = None,
) -> pd.Series:
    """风格因子中性化: 截面多元回归 alpha ~ style_factors, 取残差.

    消除已知风格因子的暴露, 使 alpha 反映的是"纯特质收益"而非"押注风格"。

    支持风格因子:
      - value (book-to-price) — Fama-French HML
      - momentum (trailing return) — Carhart MOM
      - volatility (idiosyncratic vol) — BARRA
      - quality (ROE) — BARRA

    Args:
        scores: index=symbol, alpha 得分
        exposures: {factor_name: pd.Series(index=symbol, value=exposure)}
        min_common: 最少共同股票数 (默认从 config 读取)

    Returns: 残差 = 纯特质 alpha (去均值标准化)

    来源:
      ② Fama & French (2015) — 五因子模型
      ② BARRA USE4 — 风格因子风险分解
      ② Carhart (1997) — 四因子动量
    """
    if min_common is None:
        min_common = _MIN_COMMON

    # 构建因子矩阵
    valid = scores.dropna().index
    factor_data = {}
    for name, series in exposures.items():
        aligned = series.reindex(valid).dropna()
        factor_data[name] = aligned

    # 取所有因子共有的股票
    common = valid
    for series in factor_data.values():
        common = common.intersection(series.index)

    if len(common) < min_common:
        logger.warning(f"[neutralize] style: only {len(common)} common stocks "
                       f"(< {min_common}), skip")
        return scores

    # 构建回归矩阵: [1, exposure_1, exposure_2, ...]
    y = scores.loc[common].values
    X_cols = []
    X_arr = np.ones((len(common), 1))  # intercept
    for name in factor_data:
        vals = factor_data[name].loc[common].values
        # 截面 z-score 标准化
        vals = (vals - vals.mean()) / (vals.std(ddof=1) + 1e-8)
        X_arr = np.column_stack([X_arr, vals])
        X_cols.append(name)

    if X_arr.shape[1] < 2:
        return scores

    try:
        beta = np.linalg.lstsq(X_arr, y, rcond=None)[0]
        y_pred = X_arr @ beta
        residuals = y - y_pred
    except np.linalg.LinAlgError:
        logger.warning("[neutralize] style: linear algebra error, skip")
        return scores

    result = pd.Series(residuals, index=common)
    result = (result - result.mean()) / result.std(ddof=1)
    logger.debug(f"[neutralize] style: {len(common)} stocks, "
                 f"factors={X_cols}, adj_R2≈{1 - np.var(residuals) / np.var(y):.3f}")
    return result.reindex(scores.index)


def neutralize(
    scores: pd.Series,
    industries: Optional[pd.Series] = None,
    market_caps: Optional[pd.Series] = None,
    style_exposures: Optional[dict[str, pd.Series]] = None,
) -> pd.Series:
    """统一的 alpha 中性化入口 (行业 + 市值 + 风格).

    v380: 当 industries 和 market_caps 同时提供时, 使用联合回归
    (行业哑变量 + log(mcap) → OLS 残差), 对齐 Barra USE4 标准。
    仅一项时退化为单独中性化。

    v551: 撤销单维度退化 — 行业+市值是 Barra 联合中性化硬要求 (B32),
    缺任一维度 = 风控不可执行 = 抛错阻断, 不静默降级.

    Returns: 中性化后的得分

    来源:
      ② BARRA USE4 — 多因子风险中性化标准流程 (联合回归)
      ② Grinold & Kahn (2000) Ch.4
    """
    result = scores.copy()

    if industries is None or market_caps is None:
        _missing = "industries" if industries is None else "market_caps"
        raise ValueError(
            f"neutralize: {_missing} missing — 行业+市值联合中性化是风控硬要求 "
            f"(B32), 不降级, 请检查上游数据 (daily_valuation PIT 覆盖)"
        )
    result = _joint_neutralize(result, industries, market_caps)
    logger.info("[neutralize] joint (industry+size)")

    if style_exposures:
        result = style_neutralize(result, style_exposures)

    return result


def _joint_neutralize(
    scores: pd.Series,
    industries: pd.Series,
    market_caps: pd.Series,
) -> pd.Series:
    """联合中性化: 行业哑变量 + log(市值) → OLS 残差。

    Barra USE4 标准: 所有风险因子同时回归, 避免顺序中性化引入偏差。
    行业哑变量用 get_dummies, 剔除 <3 只股票的行业 (过拟合保护)。

    来源: Barra USE4 (MSCI, 2011); Grinold & Kahn (2000) Ch.4 Eq.4.7-4.9.
    """
    common = scores.dropna().index
    common = common.intersection(market_caps.dropna().index)
    common = common.intersection(industries.dropna().index)
    if len(common) < _MIN_COMMON:
        return scores

    y = scores.loc[common].values.astype(np.float64)
    log_mcap = np.log(np.asarray(market_caps.loc[common].values, dtype=np.float64))

    # 行业哑变量 (剔除小行业)
    ind_series = industries.loc[common]
    ind_counts = ind_series.value_counts()
    valid_inds = ind_counts[ind_counts >= 3].index
    ind_series = ind_series.where(ind_series.isin(valid_inds), "other")

    ind_dummies = pd.get_dummies(ind_series, drop_first=True).astype(np.float64)
    # v380 fix: 必须含截距项 (np.ones), 否则OLS强迫过原点 → 残差有偏
    X = np.column_stack([np.ones(len(common)), log_mcap, ind_dummies.values])

    # OLS: y = β₀ + β₁·log(mcap) + Σβᵢ·industryᵢ + ε
    try:
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
        residuals = y - X @ beta
    except np.linalg.LinAlgError:
        return scores

    result = pd.Series(residuals, index=common)
    result = (result - result.mean()) / result.std(ddof=1)
    return result.reindex(scores.index)


def _build_neutralize_projection(
    industries: pd.Series,
    market_caps: pd.Series,
) -> tuple[np.ndarray, pd.Index]:
    """test-v397 (P1): 预构建中性化投影矩阵 P = I - X(X'X)^(-1)X'.

    返回 (P, common_index)。对每个因子 Series y, 残差 = P @ y[common_index]。
    原先 30 个因子各自做一次 lstsq(X, y), 现在共用一个 P, 速度 ~30x。

    industries: index=symbol, 行业分类
    market_caps: index=symbol, 总市值

    v551: 单 None 直接抛错 (撤销 v550 降级) — 市值/行业中性化是风控硬要求
    (B32), 缺数据 = 数据不完整 = 阻断暴露, 不静默降级不跳过.
    """
    if industries is None or market_caps is None:
        _missing = "industries" if industries is None else "market_caps"
        raise ValueError(
            f"_build_neutralize_projection: {_missing} is None — 中性化维度缺失, "
            f"风控不降级 (B32), 请检查上游数据 (daily_valuation PIT 覆盖)"
        )
    common = market_caps.dropna().index.intersection(industries.dropna().index)
    if len(common) < _MIN_COMMON:
        raise ValueError(f"_build_neutralize_projection: only {len(common)} common stocks")

    log_mcap = np.log(np.asarray(market_caps.loc[common].values, dtype=np.float64))
    ind_series = industries.loc[common]
    ind_counts = ind_series.value_counts()
    valid_inds = ind_counts[ind_counts >= 3].index
    ind_series = ind_series.where(ind_series.isin(valid_inds), "other")
    ind_dummies = pd.get_dummies(ind_series, drop_first=True).astype(np.float64)

    X = np.column_stack([np.ones(len(common)), log_mcap, ind_dummies.values])
    try:
        XtX_inv = np.linalg.inv(X.T @ X)
    except np.linalg.LinAlgError:
        XtX_inv = np.linalg.pinv(X.T @ X)
    H = X @ XtX_inv @ X.T  # hat matrix
    P = np.eye(len(common)) - H   # projection onto residual space
    return P.astype(np.float64), common


def _apply_neutralize_batch(
    P: np.ndarray,
    common_index: pd.Index,
    scores: pd.Series,
) -> pd.Series:
    """test-v397 (P1): 用预构建的 P 矩阵对单个因子做中性化。

    P: _build_neutralize_projection() 返回的投影矩阵
    common_index: 对应 P 的 index
    scores: 因子 z-score Series (index=symbol)

    返回: 中性化后的 z-score Series
    """
    aligned = scores.reindex(common_index)
    valid = aligned.dropna()
    if len(valid) < 2:
        return scores
    # P0-3 fix: NaN values in aligned → P @ y 传染全矩阵 NaN.
    # 丢弃 NaN 后按位置切片 P, 与标量路径 (_joint_neutralize dropna) 语义一致.
    pos = common_index.get_indexer(valid.index)
    y = valid.values.astype(np.float64)
    Pv = P[pos][:, pos]
    residuals = Pv @ y
    result = pd.Series(residuals, index=valid.index)
    result = (result - result.mean()) / result.std(ddof=1)
    return result.reindex(scores.index)


def neutralize_factors_batch(
    factor_values: dict[str, pd.Series],
    industries: pd.Series = None,
    market_caps: pd.Series = None,
) -> dict[str, pd.Series]:
    """test-v397 (P1): 批量中性化所有因子 — 共享投影矩阵, 避免逐因子 lstsq。

    factor_values: {factor_name: Series(index=symbol)}
    industries: index=symbol
    market_caps: index=symbol

    返回: {factor_name: 中性化后的 Series}
    """
    # v551: 两者都 None 也抛 — 中性化是风控硬要求 (B32), 不静默跳过 (原 return 降级)
    if industries is None and market_caps is None:
        raise ValueError(
            "_build_neutralize_projection: industries and market_caps both None — "
            "中性化维度缺失, 风控不降级 (B32)"
        )
    try:
        P, common = _build_neutralize_projection(industries, market_caps)
    except ValueError as e:
        # v551: 不静默跳过 (原 warning+skip 是降级) — 样本不足/维度缺失 = 风控
        # 不可执行 = 阻断 (B32)
        raise ValueError(f"neutralize_factors_batch: {e}") from e

    result = {}
    for fname, fseries in factor_values.items():
        if not isinstance(fseries, pd.Series) or fseries.dropna().empty:
            result[fname] = fseries
            continue
        # z-score first, then neutralize
        z = (fseries - fseries.mean()) / (fseries.std(ddof=1) + 1e-8)
        result[fname] = _apply_neutralize_batch(P, common, z)
    return result
