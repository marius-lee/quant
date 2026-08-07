"""因子合成 — 将多个因子合成为单一复合因子得分。

导出:
  equal_weight        — 等权平均
  ic_weighted         — IC 加权 (|IC| 比例)
  sleeve_compose      — 分仓合成 (每因子独立选 top N, 取并集)
  intersection_alpha  — 交集筛选 (每因子排前 X% 才进候选池)
  strict_intersection — 严格交集 (每因子取 top N, 同时出现才进池)

合成模式 (config.yaml alpha.combine_mode):
  composite — 加权压缩为单一得分 (ic_weighted / equal_weight / intersection)
  sleeve    — 每因子独立分仓, 保留因子间独立信号 (sleeve_compose)

来源: ② Grinold & Kahn (2000) Chapter 8 — Alpha 合成.
"""

import numpy as np
import pandas as pd
from quant.factor.intersection import intersection_alpha, strict_intersection  # kept in factor/


def equal_weight(factor_values: dict) -> pd.Series:
    """等权合成: 所有因子取 z-score 后等权平均。

    factor_values: {name: Series(index=symbol)} — 同日期截面的因子值
    min_factors: 至少需要的有效因子数 (默认 len//2)

    返回: Series(index=symbol), 合成得分
    来源: ② 最朴素的合成方式, 当 IC 估计不可靠时的安全选择
    """
    names = list(factor_values.keys())
    if not names:
        return pd.Series(dtype=float)

    composite = pd.DataFrame(factor_values)
    min_factors = max(1, len(names) // 2)
    composite = composite.dropna(thresh=min_factors)
    return composite.mean(axis=1)


def ic_weighted(
    factor_values: dict,
    ic_scores: dict,
    clip: float = 3.0,
) -> pd.Series:
    """IC 加权合成: 权重 ∝ |IC|。

    factor_values: {name: Series(index=symbol)}
    ic_scores: {name: IC 值} — 从 FactorStats.rank_ic_mean 获取
    clip: z-score 截断阈值, 防止极端因子值主导合成

    返回: Series(index=symbol)
    来源: ② Grinold & Kahn (2000) — IC 加权 alpha 合成
    """
    names = [n for n in factor_values if n in ic_scores]
    if not names:
        return equal_weight(factor_values)

    # v394: ic_scores 值可为 float 或 dict{ic_mean,ic_ir,weight}, 统一提取float
    def _extract_weight(v):
        if isinstance(v, dict):
            return float(v.get("weight", v.get("ic_mean", 0)))
        return float(v)
    raw_weights = np.array([_extract_weight(ic_scores[n]) for n in names])
    total = np.abs(raw_weights).sum()
    if total == 0:
        return equal_weight(factor_values)
    weights = raw_weights / total

    df = pd.DataFrame({n: factor_values[n] for n in names})
    for col in df.columns:
        mu = df[col].mean()
        sigma = df[col].std(ddof=1)
        if sigma == 0:
            df[col] = 0
            continue
        z = (df[col] - mu) / sigma
        df[col] = z.clip(-clip, clip)

    composite = (df * weights).sum(axis=1)
    return composite


import logging
_log = logging.getLogger("quant.factor.synth")

def sleeve_compose(
    factor_values: dict,
    positions_per_factor: int,
    min_factors: int,
) -> pd.Series:
    """分仓合成: 每个因子独立选取 top N 只股票, 取并集。

    与 composite 模式的本质区别: 不做维度压缩。reversal 选超跌、
    volatility 选低波、momentum 选趋势 — 不同的逻辑不应该被加权冲淡。

    test-v397 (Problem 4): 改用 mean-rank 替代 max(z-score)。
    一支被 5 个因子同时选入 top-N 的股票，排名分位应高于只被 1 个因子选入的。
    原先 max(z-score) 丢失了多因子共振信息。

    factor_values: {name: Series(index=symbol)} — 已 z-score 的截面因子值
    positions_per_factor: 每个因子选取的股票数
    min_factors: 最少有效因子数 (低于此数返回空)

    返回: Series(index=symbol), 值 = mean-rank (0~1, 越高越好)
    """
    if len(factor_values) < min_factors:
        _log.debug("sleeve_compose: %d factors < min_factors=%d, returning empty", len(factor_values), min_factors)
        return pd.Series(dtype=float)

    # 第一步: 每个因子计算截面 z-score 的 rank 分位 (0~1)
    # 第二步: 对每个因子, 标记其 top-N 股票
    # 第三步: 对每只股票, 计算其被选中的因子数 + 在这些因子中的平均 rank 分位

    all_ranks: dict[str, dict[str, float]] = {}  # factor_name → {symbol: rank_pct}
    top_sets: dict[str, set] = {}  # factor_name → set of top-N symbols

    for name, scores in factor_values.items():
        valid = scores.dropna()
        if len(valid) == 0:
            continue
        # rank 分位: 1 = 最高 z-score
        ranks = valid.rank(pct=True, ascending=True)
        all_ranks[name] = dict(zip(valid.index, ranks.values))

        top_n = min(positions_per_factor, len(valid))
        top_series = valid.nlargest(top_n)
        top_sets[name] = set(top_series.index.tolist())

    if not all_ranks:
        diag = ", ".join(f"{name}({scores.dropna().count()}/{len(scores)})"
                         for name, scores in list(factor_values.items())[:10])
        _log.warning("sleeve_compose: 0 stocks selected from %d factors: %s",
                     len(factor_values), diag)
        return pd.Series(dtype=float)

    # 对所有出现过的股票计算 mean-rank 和入选因子数
    score_map = {}
    factor_count = {}
    for name, rank_dict in all_ranks.items():
        for sym, rpct in rank_dict.items():
            score_map[sym] = score_map.get(sym, 0.0) + rpct
            # v418: 原第143行 `factor_count[sym] = factor_count.get(sym, 0)` 每轮重置计数,
            # 多因子同时入 top-N 时 count 恒 ≤1, 下方 0.2×count bonus 永远无效
            if sym in top_sets.get(name, set()):
                factor_count[sym] = factor_count.get(sym, 0) + 1

    # 只保留至少被一个因子选入 top-N 的股票
    selected_syms = set()
    for ts in top_sets.values():
        selected_syms.update(ts)
    if not selected_syms:
        diag = ", ".join(f"{name}({len(s)})" for name, s in top_sets.items())
        _log.warning("sleeve_compose: no stocks in any top-N: %s", diag)
        return pd.Series(dtype=float)
    score_map = {s: score_map[s] for s in selected_syms if s in score_map}
    factor_count = {s: factor_count.get(s, 0) for s in selected_syms}

    # mean-rank: 均值(出现在哪些因子的分位), 再乘以入选因子数加权
    result_map = {}
    for sym in score_map:
        occ = factor_count.get(sym, 0) + 1  # +1 防止全 zero
        mean_rank = score_map[sym] / occ
        # bonus for multi-factor confirmation
        result_map[sym] = mean_rank * (1.0 + 0.2 * factor_count.get(sym, 0))

    _log.info("sleeve: %d factors → %d stocks (positions_per_factor=%d, score range %.2f~%.2f)",
              len(all_ranks), len(result_map), positions_per_factor,
              min(result_map.values()), max(result_map.values()))

    result = pd.Series(result_map, name="alpha").sort_values(ascending=False)
    return result


def factor_attribution(factor_values: dict, target_symbols: list,
                       positions_per_factor: int = 10, max_factors: int = 3) -> dict:
    """返回每个目标标的的因子归因字符串 (test-v206).

    sleeve 模式: 列出该标的 z-score 最高且入选 top-N 的因子。
    用于替换 reason 字段的 #1/#2 无意义序号。

    Returns:
        dict[symbol] -> str, e.g. "momentum_63d(+2.1), reversal_5d(+1.8)"
    """
    result = {}
    for sym in target_symbols:
        contributions = []
        for name, scores in factor_values.items():
            val = scores.get(sym)
            if val is None or (isinstance(val, float) and val != val):
                continue
            valid = scores.dropna()
            top_n = min(positions_per_factor, len(valid))
            top_set = set(valid.nlargest(top_n).index.tolist())
            in_top = sym in top_set
            contributions.append((name, float(val), in_top))
        # 按入选状态优先, 再按 z-score 降序
        contributions.sort(key=lambda x: (x[2], x[1]), reverse=True)
        top_k = contributions[:max_factors]
        if top_k:
            parts = []
            for fname, fval, in_top in top_k:
                sign = "+" if fval >= 0 else ""
                marker = "*" if in_top else ""
                parts.append(f"{fname}{marker}({sign}{fval:.2f})")
            result[sym] = ", ".join(parts)
        else:
            result[sym] = "-"
    return result
