"""ML 模型共用: 特征对齐 + 样本外评估 (v423).

根因修复: 训练端直读原始因子 vs 生产端 pipeline 先中性化再喂模型 →
特征分布漂移 → 模型失效. 此处统一训练特征口径:
  1. build_cross_sectional_factors(): 逐日截面 z-score (与 pipeline 一致)
  2. split_train_oos(): 时间顺序切分 OOS (尾部 15%)
  3. daily_ic_series(): 逐日截面 IC 序列 → ICIR (业界标准: Grinold ICIR)
"""

import numpy as np
import pandas as pd

from quant.utils.logger import get_logger
from quant.utils.date import to_str

_log = get_logger("alpha.ml_common")


def build_cross_sectional_factors(
    factor_values: dict[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """逐日截面对每个因子做 z-score (与 pipeline neutralize_factors_batch 中
    z-score 步骤同口径).

    factor_values: {name: DataFrame(index=date, columns=symbol)}
    返回: 同结构, 每个截面对数行 rank→z 的数值 (稳健标准化).
    """
    result = {}
    for name, panel in factor_values.items():
        if panel is None or panel.empty:
            result[name] = panel
            continue
        # 逐日截面: 秩 → 百分比 → 标准正态分位 (robust z-score)
        ranked = panel.rank(axis=1, pct=True)
        z = ranked.apply(lambda row: _inv_norm(row.clip(0.0001, 0.9999)), axis=1)
        # 保持数值规模与 0 填充语义一致
        result[name] = z.where(panel.notna()).astype(np.float32)
    return result


def _norm_inv_cache():
    try:
        from scipy.stats import norm
        return norm.ppf
    except ImportError:
        return None


def _inv_norm(series: pd.Series) -> pd.Series:
    """秩→标准正态 z. scipy 不可用时退化 (0.5-0.5)/0.5 线性近似."""
    ppf = _norm_inv_cache()
    if ppf is not None:
        return pd.Series(ppf(series.values), index=series.index)
    return (series - 0.5) * 6.0  # 线性近似, 边缘更平


def split_train_oos(dates: list, oos_frac: float = 0.15) -> tuple[set, set]:
    """时间顺序切分: 前 (1-frac) 做训练, 尾 frac 做 OOS (可空集).

    返回: (train_date_set, oos_date_set)   边界语义: 严格不相交.
    输入 dates 为 str ('YYYY-MM-DD') 或 datetime, 统一按 str 比较.
    """
    if len(dates) == 0:
        return set(), set()
    n = max(int(len(dates) * (1 - oos_frac)), 1)
    sorted_dates = sorted({to_str(d) for d in dates})
    train = set(sorted_dates[:n])
    oos = set(sorted_dates[n:])
    return train, oos


def daily_ic_series(
    pred: np.ndarray,
    actual: np.ndarray,
    date_labels: list,
) -> dict:
    """以逐日截面 IC 序列汇总 OOS 效果 (业界标准: ICIR = mean/std).

    pred/actual: 对齐的行数组 (OOS 样本), date_labels: 每行所属日期.
    返回: {ic_mean, ic_std, icir, n_days}
    """
    pairs: dict[str, list] = {}
    for d, p, a in zip(date_labels, pred, actual):
        pairs.setdefault(str(d), ([], []))
        pairs[str(d)][0].append(float(p))
        pairs[str(d)][1].append(float(a))

    ics = []
    for d, (ps, as_) in pairs.items():
        if len(ps) < 20:
            continue
        p_arr = np.asarray(ps, dtype=float)
        a_arr = np.asarray(as_, dtype=float)
        if np.std(p_arr) < 1e-9 or np.std(a_arr) < 1e-9:
            continue
        r = np.corrcoef(p_arr, a_arr)[0, 1]
        if np.isfinite(r):
            ics.append(r)
    if not ics:
        return {"ic_mean": 0.0, "ic_std": 0.0, "icir": 0.0, "n_days": 0}
    ic_arr = np.asarray(ics)
    ic_mean = float(np.mean(ic_arr))
    ic_std = float(np.std(ic_arr, ddof=1))
    icir = float(ic_mean / ic_std) if ic_std > 1e-12 else 0.0
    return {"ic_mean": round(ic_mean, 4), "ic_std": round(ic_std, 6),
            "icir": round(icir, 3), "n_days": len(ic_arr)}


def build_train_matrices(
    factor_values: dict[str, pd.DataFrame],
    forward_returns: pd.Series,
    feature_names: list[str],
    oos_frac: float = 0.15,
    min_features_frac: float = 0.6,
    min_symbols: int = 30,
    min_labels: int = 20,
) -> dict:
    """统一训练矩阵构建: 时间顺序切分 train/OOS + 逐日 z-score 特征 (v423).

    与 LGB/XGB 两模型共用, 消除训练/推理特征分布漂移.
    factor_values: {name: DataFrame(index=date, columns=symbol)}
    forward_returns: Series(MultiIndex date,symbol)
    返回:
      {X_tr, y_tr, X_oo, y_oo, oos_dates, train_dates, skipped}
    X_tr/X_oo: float32 矩阵; y_*: 对应标签; oos_dates/train_dates: 行数对齐的日期标签
    """
    from quant.alpha.ml_common import build_cross_sectional_factors, split_train_oos
    _zs = build_cross_sectional_factors(factor_values)

    fwd_dates = sorted(set(forward_returns.index.get_level_values(0)))
    min_features = max(1, int(len(feature_names) * min_features_frac))
    tr_set, oo_set = split_train_oos(fwd_dates, oos_frac=oos_frac)
    _log.info("train matrices: %d dates → %d train / %d OOS",
              len(fwd_dates), len(tr_set), len(oo_set))

    skipped = {"train": {"date": 0, "syms": 0, "mask": 0},
               "oos": {"date": 0, "syms": 0, "mask": 0}}

    X_tr, y_tr, d_tr = [], [], []
    X_oo, y_oo, d_oo = [], [], []

    for ts in fwd_dates:
        ds = to_str(ts)
        if ds not in tr_set and ds not in oo_set:
            continue
        bucket = "train" if ds in tr_set else "oos"
        syms = set()
        n_avail = 0
        for fn in feature_names:
            fv = _zs.get(fn)
            if fv is not None and ds in fv.index:
                row = fv.loc[ds].dropna()
                syms.update(row.index)
                n_avail += 1
        if n_avail < min_features_frac * len(feature_names):
            skipped[bucket]["date"] += 1
            continue
        syms = list(syms)
        if len(syms) < min_symbols:
            skipped[bucket]["syms"] += 1
            continue

        X_day_cols = []
        for fn in feature_names:
            fv = _zs.get(fn)
            if fv is not None and ds in fv.index:
                X_day_cols.append(fv.loc[ds].reindex(syms).fillna(0).values)
            else:
                X_day_cols.append(np.zeros(len(syms), dtype=np.float32))
        X_day = np.column_stack(X_day_cols).astype(np.float32)
        y_day_raw = forward_returns.loc[ts].reindex(syms)
        mask = y_day_raw.notna().values
        if mask.sum() < min_labels:
            skipped[bucket]["mask"] += 1
            continue
        y_day = y_day_raw.fillna(0).values.astype(np.float32)
        X_day = X_day[mask]
        n = mask.sum()
        if bucket == "train":
            X_tr.append(X_day); y_tr.append(y_day[mask])
            d_tr.extend([ts] * n)
        else:
            X_oo.append(X_day); y_oo.append(y_day[mask])
            d_oo.extend([ts] * n)

    if not X_tr:
        raise ValueError("No valid training samples — check factor_values/forward_returns")

    return {
        "X_tr": np.vstack(X_tr).astype(np.float32),
        "y_tr": np.concatenate(y_tr).astype(np.float32),
        "X_oo": np.vstack(X_oo).astype(np.float32) if X_oo else np.zeros((0, len(feature_names)), dtype=np.float32),
        "y_oo": np.concatenate(y_oo).astype(np.float32) if y_oo else np.zeros(0, dtype=np.float32),
        "oos_dates": d_oo,
        "train_dates": d_tr,
        "skipped": skipped,
    }