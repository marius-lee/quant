"""WorldQuant 风格自动因子生成。

用基础算子随机组合生成候选因子，IC 筛选保留有效因子。
支持公式回放: 首批随机生成+IC→存档公式，后续批直接回放，跳过随机和IC。
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


def _compute_one(name: str, base: pd.DataFrame, op: str, d: int,
                 close: pd.DataFrame, ret: pd.DataFrame, vol: pd.DataFrame) -> pd.DataFrame:
    """计算单个因子并Z-score标准化。返回(name, DataFrame)或None。"""
    try:
        if op == "rank":         f = _rank(base)
        elif op == "delta":      f = _delta(base, d)
        elif op == "ts_mean":    f = _ts_mean(base, d)
        elif op == "ts_std":     f = _ts_std(base, d)
        elif op == "ts_max":     f = _ts_max(base, d)
        elif op == "ts_min":     f = _ts_min(base, d)
        elif op == "scale":      f = _scale(base)
        elif op == "log":        f = np.log(base.abs() + 1)
        elif op == "abs":        f = base.abs()
        elif op == "momentum":   f = close.pct_change(d)
        elif op == "vol_ratio":  f = vol / vol.rolling(d).mean()
        else:                    return None
        f = f.replace([np.inf, -np.inf], np.nan)
        if f.isna().all().all():
            return None
        cs_mean = f.mean(axis=1)
        cs_std = f.std(axis=1).replace(0, 1)
        return name, f.sub(cs_mean, axis=0).div(cs_std, axis=0)
    except Exception:
        return None


def generate(close: pd.DataFrame, volume: pd.DataFrame = None,
             high: pd.DataFrame = None, low: pd.DataFrame = None,
             n_factors: int = 100, n_keep: int = 20,
             formulas: dict = None,
             locked_names: list = None) -> pd.DataFrame:
    """生成候选因子。

    formulas: 若传入 {name: (base_idx, op, d)}，直接回放，跳过随机&IC筛选。
    locked_names: 随机模式下列名过滤。与 formulas 互斥。
    返回: (date,stock) × factor MultiIndex DataFrame。
    """
    ret = close.pct_change()
    vol = volume if volume is not None else close * 1e7
    bases = [close, ret, vol]

    # ---- 回放模式 ----
    if formulas:
        results = {}
        for name, (bi, op, d) in formulas.items():
            r = _compute_one(name, bases[bi], op, d, close, ret, vol)
            if r is not None:
                nm, df = r
                results[nm] = df
        if not results:
            return pd.DataFrame(), {}
        result = pd.concat(results.values(), axis=1, keys=results.keys())
        result.index.name = "date"
        logger.info(f"alpha factory: replayed {len(results)}/{len(formulas)} formulas")
        return result.stack(level=1, future_stack=True).round(6), {}

    # ---- 随机生成模式 ----
    rng = np.random.RandomState(42)
    ops = ["rank", "delta", "ts_mean", "ts_std", "ts_max", "ts_min",
           "scale", "log", "abs", "momentum", "vol_ratio"]
    w = [5, 10, 20, 60]

    candidates = {}    # {name: (base_idx, op, d, df)}
    formulas_out = {}  # {name: (base_idx, op, d)}  供外部存档
    for i in range(n_factors):
        name = f"alpha_{i:03d}"
        base_idx = rng.randint(0, len(bases))
        base = bases[base_idx]
        op = ops[rng.randint(0, len(ops))]
        d = rng.choice(w) if op in ("delta","ts_mean","ts_std","ts_max","ts_min","momentum","vol_ratio") else 0

        r = _compute_one(name, base, op, d, close, ret, vol)
        if r is not None:
            nm, df = r
            candidates[nm] = (base_idx, op, d, df)
            formulas_out[nm] = (base_idx, op, d)

    if not candidates:
        logger.warning("alpha factory: no candidates generated")
        return pd.DataFrame(), {}

    # 名过滤（locked_names模式下跳过IC筛选）
    if locked_names is not None:
        kept_names = [n for n in locked_names if n in candidates]
        logger.info(f"alpha factory: locked {len(kept_names)}/{len(locked_names)} factors matched")
        if not kept_names:
            return pd.DataFrame(), {}
    else:
        # ---- IC 筛选 ----
        logger.info(f"alpha factory: generated {len(candidates)} candidates")
        from config.loader import get as cfg
        target = cfg("strategy.target", "return_1d")
        target_map = {"return_1d": 1, "return_5d": 5, "return_20d": 20}
        target_days = target_map.get(target, 1)
        ys = close.pct_change(target_days).shift(-target_days).stack(future_stack=True)

        alpha_results = []
        for name, (bi, op, d, df) in candidates.items():
            stacked = df.stack(future_stack=True)
            common = stacked.index.intersection(ys.index)
            if len(common) < 100:
                continue
            daily_ics = []
            for date, group in stacked.loc[common].groupby(level=0):
                y_date = ys.loc[group.index]
                if len(group) < 30:
                    continue
                ic = group.corr(y_date)
                if not np.isnan(ic):
                    daily_ics.append(ic)
            ic = np.mean(daily_ics) if daily_ics else 0.0
            alpha_results.append((name, abs(ic), ic))
        alpha_results.sort(key=lambda x: x[1], reverse=True)

        passed = [(n, a, ic) for n, a, ic in alpha_results if a > 0.01]
        if passed:
            kept_names = [n for n, _, _ in passed[:n_keep]]
        else:
            kept_names = [n for n, _, _ in alpha_results[:n_keep]]
            logger.warning(f"alpha factory: no candidates passed IC filter ({len(alpha_results)} valid), using top {len(kept_names)} by abs IC")

        logger.info(f"alpha factory: kept {len(kept_names)}/{len(candidates)} | "
                    f"IC={alpha_results[0][2]:.4f} ~ {alpha_results[-1][2]:.4f}")

    # 构建 (date,stock) × factor MultiIndex，只返回选中因子的公式
    kept_formulas = {n: (candidates[n][0], candidates[n][1], candidates[n][2]) for n in kept_names if n in candidates}
    all_vals = [candidates[n][3] for n in kept_names if n in candidates]
    if not all_vals:
        return pd.DataFrame(), {}
    result = pd.concat(all_vals, axis=1, keys=kept_names)
    result.index.name = "date"
    return result.stack(level=1, future_stack=True).round(6), kept_formulas
