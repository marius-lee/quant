"""因子评估缓存 — 为 Web 前端的因子分析页面提供预计算数据。

计算成本高（需遍历历史数据算 IC/IR/相关性），每次刷新页面不应该重算。
缓存到 data/factor_cache.json，默认 24h 过期。

用法:
  from factor.stats_cache import get_cached_factor_stats
  stats = get_cached_factor_stats()  # 返回前端需要的 dict
"""

import json
import os
import time
from datetime import datetime

import numpy as np
import pandas as pd

from utils.logger import get_logger

logger = get_logger("factor.stats_cache")

CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "factor_cache.json"
)
CACHE_TTL_SEC = 86400  # 24 小时


def compute_factor_stats(
    symbols: list = None, n_symbols: int = 300, lookback: int = 120
) -> dict:
    """计算所有已注册因子的评估统计量，返回前端可用格式。

    在 n_symbols 只股票的历史数据上，计算:
      - 截面 Rank IC (均值、IR)
      - IC 衰减 [1, 5, 20]
      - 因子截面相关性矩阵

    返回格式与前端 app.js generateDemoFactors() 一致:
    {
      "factors": ["动量10d", "波动率20d", ...],
      "ic": [0.032, 0.028, ...],
      "ic_ir": [0.35, 0.28, ...],
      "decay": {"动量10d": [0.032, 0.018, 0.005], ...},
      "corr": [[1, 0.6, ...], [...], ...],
      "meta": {"momentum_10d": {"category": "动量", "source": "Jegadeesh & Titman (1993)"}, ...},
      "cached_at": "2026-07-02T15:30:00"
    }
    """
    from data.store import DataStore
    from factor.compute import compute_all_factors, FACTOR_REGISTRY, FUNDAMENTAL_FACTOR_REGISTRY

    store = DataStore()

    # 1. 选择样本股票
    if symbols is None:
        conn = store._connect()
        # 取日成交额最大的 n_symbols 只股票（保证流动性）
        rows = conn.execute("""
            SELECT symbol, AVG(amount) as avg_amt
            FROM daily
            WHERE date >= date('now', '-120 days')
            GROUP BY symbol
            HAVING COUNT(*) >= 60
            ORDER BY avg_amt DESC
            LIMIT ?
        """, (n_symbols,)).fetchall()
        symbols = [r[0] for r in rows]

    if not symbols:
        logger.warning("No symbols available for factor evaluation")
        return _empty_result()

    # 2. 加载历史日线
    end_date = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - pd.Timedelta(days=lookback * 1.5)).strftime("%Y-%m-%d")
    data = store.get_daily(symbols, start=start_date, end=end_date)

    if data.empty:
        logger.warning("No daily data available for factor evaluation")
        store.close()
        return _empty_result()

    # 3. 逐日计算因子值
    dates = data.index
    factor_names = list(FACTOR_REGISTRY.keys()) + list(FUNDAMENTAL_FACTOR_REGISTRY.keys())
    factor_values_by_date = {name: {} for name in factor_names}

    # 只计算最近 lookback 个交易日
    eval_dates = dates[-lookback:]

    from tqdm import tqdm
    for d in tqdm(eval_dates, desc="Computing factors"):
        date_str = d.strftime("%Y-%m-%d") if hasattr(d, 'strftime') else str(d)[:10]
        try:
            fv = compute_all_factors(data, date_str)
            for name in factor_names:
                if name in fv and not fv[name].dropna().empty:
                    factor_values_by_date[name][date_str] = fv[name]
        except Exception as e:
            logger.warning(f"Factor compute failed at {date_str}: {e}")

    # 4. 计算前瞻收益 (用于 IC 评估)
    close = data["close"]
    # 确保 close 是 DataFrame
    if isinstance(close, pd.Series):
        close = close.unstack()  # MultiIndex to DataFrame
    forward_1d = close.pct_change().shift(-1)  # t+1 收益
    forward_5d = close.pct_change(5).shift(-5)
    forward_20d = close.pct_change(20).shift(-20)

    # 5. 计算每个因子的 IC/IR
    ic_means = {}
    ic_irs = {}
    ic_decay = {}

    for name in factor_names:
        fv_dict = factor_values_by_date[name]
        if len(fv_dict) < 20:
            continue

        # 逐截面 Rank IC
        ics = []
        for date_str, fv_series in fv_dict.items():
            if date_str not in forward_1d.index:
                continue
            fr = forward_1d.loc[date_str].dropna()
            if isinstance(fr, pd.DataFrame):
                fr = fr.iloc[0]  # 取第一个symbol的返回
            common = fv_series.dropna().index.intersection(fr.dropna().index)
            if len(common) < 30:
                continue
            from scipy import stats
            rho, _ = stats.spearmanr(fv_series.loc[common], fr.loc[common])
            if not np.isnan(rho):
                ics.append(rho)

        if ics:
            ic_arr = np.array(ics)
            ic_means[name] = float(np.mean(ic_arr))
            ic_irs[name] = float(np.mean(ic_arr) / np.std(ic_arr, ddof=1)) if np.std(ic_arr, ddof=1) > 0 else 0.0
        else:
            ic_means[name] = 0.0
            ic_irs[name] = 0.0

        # IC 衰减 (简化: 按 horizon 分组)
        decay = {}
        for horizon, fwd_df in [("1d", forward_1d), ("5d", forward_5d), ("20d", forward_20d)]:
            h_ics = []
            for date_str, fv_series in fv_dict.items():
                if date_str not in fwd_df.index:
                    continue
                fr = fwd_df.loc[date_str].dropna()
                if isinstance(fr, pd.DataFrame):
                    fr = fr.iloc[0]
                common = fv_series.dropna().index.intersection(fr.dropna().index)
                if len(common) < 30:
                    continue
                from scipy import stats
                rho, _ = stats.spearmanr(fv_series.loc[common], fr.loc[common])
                if not np.isnan(rho):
                    h_ics.append(rho)
            decay[horizon] = round(float(np.mean(h_ics)), 4) if h_ics else 0.0
        ic_decay[name] = decay

    # 6. 计算因子相关性矩阵
    corr_matrix = np.eye(len(factor_names))
    common_dates = None
    for name in factor_names:
        dates_set = set(factor_values_by_date[name].keys())
        if common_dates is None:
            common_dates = dates_set
        else:
            common_dates &= dates_set

    if common_dates:
        corr_sum = np.zeros((len(factor_names), len(factor_names)))
        n_corr = 0
        for d in sorted(common_dates):
            series = []
            for name in factor_names:
                if d in factor_values_by_date[name]:
                    series.append(factor_values_by_date[name][d])
                else:
                    break
            if len(series) != len(factor_names):
                continue
            df_corr = pd.concat(series, axis=1, keys=factor_names).dropna()
            if len(df_corr) < 30:
                continue
            corr_sum += df_corr.corr(method="spearman").values
            n_corr += 1
        if n_corr > 0:
            corr_matrix = corr_sum / n_corr

    store.close()

    # 7. 生成因子元信息
    display_names = {
        "momentum_10d": "动量10d",
        "volatility_20d": "波动率20d",
        "skewness_20d": "偏度20d",
        "bp_ratio": "BP比率",
        "size": "规模",
        "roe_ratio": "ROE比率",
    }
    categories = {
        "momentum_10d": "动量",
        "volatility_20d": "低波动",
        "skewness_20d": "偏度",
        "bp_ratio": "价值",
        "size": "规模",
        "roe_ratio": "盈利",
    }
    sources = {
        "momentum_10d": "Jegadeesh & Titman (1993)",
        "volatility_20d": "Andersen et al. (2001)",
        "skewness_20d": "Barberis & Huang (2008)",
        "bp_ratio": "Fama & French (1992)",
        "size": "Fama & French (1993)",
        "roe_ratio": "Fama & French (2015)",
    }

    meta = {}
    for name in factor_names:
        meta[name] = {
            "display": display_names.get(name, name),
            "category": categories.get(name, "未知"),
            "source": sources.get(name, "—"),
            "n_periods": len(factor_values_by_date.get(name, {})),
        }

    # 8. 组装返回
    display_factor_names = [meta[n]["display"] for n in factor_names]
    result = {
        "factors": display_factor_names,
        "factor_keys": factor_names,
        "ic": [round(ic_means.get(n, 0.0), 4) for n in factor_names],
        "ic_ir": [round(ic_irs.get(n, 0.0), 2) for n in factor_names],
        "decay": {
            meta[n]["display"]: [
                ic_decay.get(n, {}).get("1d", 0.0),
                ic_decay.get(n, {}).get("5d", 0.0),
                ic_decay.get(n, {}).get("20d", 0.0),
            ]
            for n in factor_names
        },
        "corr": corr_matrix.round(4).tolist(),
        "meta": meta,
        "cached_at": datetime.now().isoformat(),
    }
    return result


def _empty_result() -> dict:
    """返回空结果（数据不足时）。"""
    from factor.compute import get_factor_names
    names = get_factor_names()
    return {
        "factors": names,
        "factor_keys": names,
        "ic": [0.0] * len(names),
        "ic_ir": [0.0] * len(names),
        "decay": {n: [0.0, 0.0, 0.0] for n in names},
        "corr": np.eye(len(names)).tolist(),
        "meta": {n: {"display": n, "category": "—", "source": "—", "n_periods": 0} for n in names},
        "cached_at": datetime.now().isoformat(),
    }


def get_cached_factor_stats(force_refresh: bool = False) -> dict:
    """获取缓存的因子评估数据。缓存过期或 force_refresh=True 时重新计算。

    返回: compute_factor_stats() 的输出格式
    """
    if not force_refresh and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                cached = json.load(f)
            cached_at = datetime.fromisoformat(cached.get("cached_at", "2000-01-01"))
            age_sec = (datetime.now() - cached_at).total_seconds()
            if age_sec < CACHE_TTL_SEC:
                logger.info(f"factor cache hit, age={age_sec/60:.0f}min")
                return cached
            logger.info(f"factor cache expired, age={age_sec/3600:.1f}h")
        except Exception as e:
            logger.warning(f"Factor cache read failed: {e}")

    # 重新计算
    logger.info("computing factor stats (this may take ~30s)...")
    stats = compute_factor_stats(n_symbols=200, lookback=90)

    # 缓存到文件
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        logger.info(f"factor cache saved to {CACHE_FILE}")
    except Exception as e:
        logger.warning(f"Factor cache write failed: {e}")

    return stats
