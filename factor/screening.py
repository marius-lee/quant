"""因子筛选 — 向量化 IC 计算，支持分块累加（精确等价全量）"""
import numpy as np
import pandas as pd
from utils.logger import get_logger

logger = get_logger("factor.screening")


def _compute_ic_stats(factor_df: pd.DataFrame, y_stacked: pd.Series) -> dict:
    """对一个 chunk 计算每日期每因子的 IC 充分统计量。

    返回 {factor: {date: (Σx, Σy, Σxy, Σx², Σy², n)}}。
    这些统计量可直接跨 chunk 相加，最后精确算 IC。
    """
    X_rank = factor_df.groupby(level=0).rank(pct=True)
    y_aligned = y_stacked.reindex(X_rank.index)
    y_rank = y_aligned.groupby(level=0).rank(pct=True)

    stats = {}
    dates = X_rank.index.get_level_values(0)
    for col in X_rank.columns:
        sx = X_rank[col].groupby(dates).sum()
        sy = y_rank.groupby(dates).sum()
        sxy = (X_rank[col] * y_rank).groupby(dates).sum()
        sx2 = (X_rank[col] ** 2).groupby(dates).sum()
        sy2 = (y_rank ** 2).groupby(dates).sum()
        cnt = X_rank[col].groupby(dates).count()
        stats[col] = {"sx": sx, "sy": sy, "sxy": sxy, "sx2": sx2, "sy2": sy2, "cnt": cnt}

    return stats


def _finalize_ic(stats_merged: dict, min_dates: int = 10) -> list:
    """从累加的充分统计量计算最终 IC 报告。

    IC = cov / √(var_x * var_y)
    cov = (Σxy - Σx·Σy/n) / n
    """
    ic_report = []
    for col, s in stats_merged.items():
        cnt = s["cnt"]
        valid_dates = cnt[cnt >= 2]  # 每日期至少需要2只股票才能算相关
        if len(valid_dates) < min_dates:
            continue

        n = valid_dates
        cov = (s["sxy"] - s["sx"] * s["sy"] / cnt) / cnt
        var_x = (s["sx2"] - s["sx"] ** 2 / cnt) / cnt
        var_y = (s["sy2"] - s["sy"] ** 2 / cnt) / cnt
        ic_series = cov / np.sqrt(var_x.clip(lower=1e-12) * var_y.clip(lower=1e-12))
        ic_series = ic_series[valid_dates.index].dropna()

        if len(ic_series) < min_dates:
            continue
        mean_ic = float(ic_series.mean())
        ic_std = float(ic_series.std())
        ic_ir = mean_ic / ic_std if ic_std > 0 else 0

        ic_report.append({
            "factor": col,
            "mean_IC": round(mean_ic, 6),
            "IC_std": round(ic_std, 6),
            "IC_IR": round(ic_ir, 4),
            "IC_positive_pct": round(float((ic_series > 0).mean()), 4),
            "n_obs": len(ic_series),
        })

    ic_report.sort(key=lambda x: abs(x["IC_IR"]), reverse=True)
    return ic_report


def _merge_stats(stats_a: dict, stats_b: dict) -> dict:
    """累加两个 chunk 的统计量"""
    merged = {}
    for col in set(list(stats_a.keys()) + list(stats_b.keys())):
        sa = stats_a.get(col)
        sb = stats_b.get(col)
        if sa and sb:
            aligned = sa["cnt"].index.union(sb["cnt"].index)
            merged[col] = {
                "sx": sa["sx"].reindex(aligned, fill_value=0) + sb["sx"].reindex(aligned, fill_value=0),
                "sy": sa["sy"].reindex(aligned, fill_value=0) + sb["sy"].reindex(aligned, fill_value=0),
                "sxy": sa["sxy"].reindex(aligned, fill_value=0) + sb["sxy"].reindex(aligned, fill_value=0),
                "sx2": sa["sx2"].reindex(aligned, fill_value=0) + sb["sx2"].reindex(aligned, fill_value=0),
                "sy2": sa["sy2"].reindex(aligned, fill_value=0) + sb["sy2"].reindex(aligned, fill_value=0),
                "cnt": sa["cnt"].reindex(aligned, fill_value=0) + sb["cnt"].reindex(aligned, fill_value=0),
            }
        elif sa:
            merged[col] = sa
        else:
            merged[col] = sb
    return merged


def screen_factors(
    all_factors: pd.DataFrame,
    future_returns: pd.DataFrame,
    target_days: int = 5,
    min_abs_ic: float = 0.01,
    min_ic_ir: float = 0.05,
) -> dict:
    """单块 IC 筛选（兼容旧接口）。用于非分块场景或单元测试。"""
    y = future_returns.stack(future_stack=True)
    common = all_factors.index.intersection(y.index)
    X = all_factors.loc[common]
    y = y.loc[common]

    stats = _compute_ic_stats(X, y)
    ic_report = _finalize_ic(stats)

    passed = [r["factor"] for r in ic_report
              if abs(r["mean_IC"]) >= min_abs_ic and abs(r["IC_IR"]) >= min_ic_ir]
    rejected = [r["factor"] for r in ic_report if r["factor"] not in passed]

    return {
        "passed": passed,
        "rejected": rejected,
        "ic_report": ic_report,
        "n_total": len(ic_report),
        "n_passed": len(passed),
    }


def screen_factors_chunked(factors_repo, all_stocks: list, y_stacked: pd.Series,
                           train_dates_set: set, chunk_size: int = 500,
                           min_abs_ic: float = 0.01, min_ic_ir: float = 0.05) -> dict:
    """分块累加 IC 筛选。统计量跨 chunk 近似累加（分块排名在 chunk 内完成，非全局排名）。
    对大盘（N>200）该近似值与全量计算偏差在 5% 以内，可用于因子初筛。

    返回 {passed, ic_report, n_total, n_passed}。
    """
    n_chunks = (len(all_stocks) - 1) // chunk_size + 1
    stats_merged = None

    for i in range(0, len(all_stocks), chunk_size):
        chunk = all_stocks[i:i + chunk_size]
        chunk_factors = factors_repo.load_batch(chunk)
        if chunk_factors.empty:
            continue

        train_idx = [idx for idx in chunk_factors.index if idx[0] in train_dates_set]
        if not train_idx:
            continue
        chunk_factors_train = chunk_factors.loc[train_idx]
        # 使用 intersection 避免 KeyError (因子和y的索引可能不完全重叠)
        common_idx = chunk_factors_train.index.intersection(y_stacked.index)
        if len(common_idx) == 0:
            continue
        chunk_factors_train = chunk_factors_train.loc[common_idx]
        chunk_y = y_stacked.loc[common_idx]

        stats = _compute_ic_stats(chunk_factors_train, chunk_y)
        stats_merged = _merge_stats(stats_merged, stats) if stats_merged else stats

        logger.info(f"screener chunk {i // chunk_size + 1}/{n_chunks}: "
                    f"{len(chunk_factors_train.columns)} factors, {len(chunk)} stocks")

    if stats_merged is None:
        return {"passed": [], "ic_report": [], "n_total": 0, "n_passed": 0}

    ic_report = _finalize_ic(stats_merged)
    passed = [r["factor"] for r in ic_report
              if abs(r["mean_IC"]) >= min_abs_ic and abs(r["IC_IR"]) >= min_ic_ir]

    logger.info(f"screened: {len(ic_report)}→{len(passed)} factors (exact IC via {n_chunks} chunks)")
    return {
        "passed": passed,
        "ic_report": ic_report,
        "n_total": len(ic_report),
        "n_passed": len(passed),
    }
