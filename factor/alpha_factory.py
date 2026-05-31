"""WorldQuant 风格自动因子生成。

用基础算子随机组合生成候选因子，IC 筛选保留有效因子。
"""
import numpy as np
import pandas as pd
from utils.logger import get_logger

logger = get_logger("factor.alpha_factory")


def _rank(x: pd.DataFrame) -> pd.DataFrame:
    return x.rank(axis=1, pct=True)


def _delta(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x - x.shift(d)


def _ts_mean(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d).mean()


def _ts_std(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d).std()


def _ts_max(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d).max()


def _ts_min(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d).min()


def _scale(x: pd.DataFrame) -> pd.DataFrame:
    return x.div(x.abs().sum(axis=1), axis=0)


def generate(close: pd.DataFrame, volume: pd.DataFrame = None,
             high: pd.DataFrame = None, low: pd.DataFrame = None,
             n_factors: int = 100, n_keep: int = 20) -> pd.DataFrame:
    """生成候选因子, IC 筛选后保留 top n_keep, 返回 (date,stock) × factor MultiIndex DataFrame"""
    ret = close.pct_change()
    vol = volume if volume is not None else close * 1e7
    w = [5, 10, 20, 60]
    rng = np.random.RandomState(42)

    candidates = {}
    for i in range(n_factors):
        try:
            base = rng.choice([close, ret, vol])
            op = rng.choice(["rank", "delta", "ts_mean", "ts_std", "ts_max", "ts_min",
                             "scale", "log", "abs", "momentum", "vol_ratio"])

            if op == "rank":
                f = _rank(base)
            elif op == "delta":
                d = rng.choice(w)
                f = _delta(base, d)
            elif op == "ts_mean":
                d = rng.choice(w)
                f = _ts_mean(base, d)
            elif op == "ts_std":
                d = rng.choice(w)
                f = _ts_std(base, d)
            elif op == "ts_max":
                d = rng.choice(w)
                f = _ts_max(base, d)
            elif op == "ts_min":
                d = rng.choice(w)
                f = _ts_min(base, d)
            elif op == "scale":
                f = _scale(base)
            elif op == "log":
                f = np.log(base.abs() + 1)
            elif op == "abs":
                f = base.abs()
            elif op == "momentum":
                d = rng.choice(w)
                f = close.pct_change(d)
            else:  # vol_ratio
                d = rng.choice(w)
                f = vol / vol.rolling(d).mean()

            f = f.replace([np.inf, -np.inf], np.nan)
            if f.isna().all().all():
                continue

            # 截面 Z-score 标准化
            cs_mean = f.mean(axis=1)
            cs_std = f.std(axis=1).replace(0, 1)
            f = f.sub(cs_mean, axis=0).div(cs_std, axis=0)

            candidates[f"alpha_{i:03d}"] = f
        except Exception:
            logger.warning(f"alpha candidate {i} failed, skipping")
            continue

    if not candidates:
        logger.warning("alpha factory: no candidates generated")
        return pd.DataFrame()

    logger.info(f"alpha factory: generated {len(candidates)} candidates")

    # IC 筛选 - 使用 config 驱动目标
    from config.loader import get as cfg
    target = cfg("strategy.target", "return_1d")
    target_map = {"return_1d": 1, "return_5d": 5, "return_20d": 20}
    target_days = target_map.get(target, 1)
    future_ret = close.pct_change(target_days).shift(-target_days)
    ys = future_ret.stack(future_stack=True)

    alpha_results = []
    for name, factor_df in candidates.items():
        stacked = factor_df.stack(future_stack=True)
        common = stacked.index.intersection(ys.index)
        if len(common) < 100:
            continue
        ic = stacked.loc[common].corr(ys.loc[common])
        if not np.isnan(ic) and abs(ic) > 0.01:
            alpha_results.append((name, abs(ic), ic))

    alpha_results.sort(key=lambda x: x[1], reverse=True)

    if not alpha_results:
        logger.warning("alpha factory: no candidates passed IC filter, using top 5 by abs IC")
        # 对所有候选因子计算IC，按abs(IC)排序取top n_keep
        for name, factor_df in candidates.items():
            stacked = factor_df.stack(future_stack=True)
            common = stacked.index.intersection(ys.index)
            if len(common) < 50:
                continue
            ic = stacked.loc[common].corr(ys.loc[common])
            if not np.isnan(ic):
                alpha_results.append((name, abs(ic), ic))
        alpha_results.sort(key=lambda x: x[1], reverse=True)

    kept = alpha_results[:n_keep] if alpha_results else []
    logger.info(f"alpha factory: kept {len(kept)}/{len(candidates)} | IC={kept[0][2]:.4f} ~ {kept[-1][2]:.4f}")

    # 构建 (date,stock) × factor MultiIndex
    all_vals = []
    all_cols = []
    for name, _, _ in kept:
        df = candidates[name]
        for s in df.columns:
            all_cols.append((name, s))
        all_vals.append(df)

    result = pd.concat(all_vals, axis=1)
    result.index.name = "date"
    result.columns = pd.MultiIndex.from_tuples(all_cols)
    result = result.stack(level=1, future_stack=True).round(6)
    return result
