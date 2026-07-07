"""因子评估缓存 — 为 Web 前端的因子分析页面提供预计算数据。

计算成本高（需遍历历史数据算 IC/IR/相关性），每次刷新页面不应该重算。
评估结果存入 factor_snapshot 表，24h 过期自动重算。

benchmark (模板 5): ~2.5s/factor @ 800 stocks × 120 dates (M1 Max).
regression threshold: >5.0s/factor 时排查 (索引丢失 / O(n²)退化 / 磁盘IO瓶颈).

参数依据: n_symbols=800 对标中证800 (A股量化策略标准基准, 中证指数有限公司);
lookback=120 对标国内券商因子研报惯例 (过去120个交易日 ≈ 半年),
t = |IR| × √n 提供 |IR|≥0.18 的最小可检测效应 (Grinold & Kahn 1999 第6章).

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
from config.loader import get as _cfg
from factor.compute import _require_cfg

logger = get_logger("factor.stats_cache")

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
_SNAPSHOT_TTL_SEC = _cfg("factor.stats.snapshot_ttl_sec", 86400)  # 24h


def compute_factor_stats(
    symbols: list = None, n_symbols: int = None, lookback: int = None,
    factor_names: list = None,
) -> dict:
    """计算所有已注册因子的评估统计量，返回前端可用格式。

    n_symbols / lookback 默认值来源: config.yaml factor.evaluation (单一真相源).
    fallback: n_symbols=800 (中证800), lookback=120 (券商研报惯例).
    """
    if n_symbols is None:
        n_symbols = _require_cfg("factor.evaluation.n_symbols")
    if lookback is None:
        lookback = _require_cfg("factor.evaluation.lookback")

    from data.store import DataStore
    from factor.compute import compute_all_factors, get_factor_names

    store = DataStore()

    # 1. 选择样本股票
    if symbols is None:
        conn = store._connect()
        # 取日成交额最大的 n_symbols 只股票（保证流动性）
        # 用 lookback 参数化选股窗口: 回看 lookback 个日历日, 至少交易一半天数
        stock_window = int(lookback * 1.5)
        min_days = max(30, lookback // 2)
        if n_symbols and n_symbols > 0:
            rows = conn.execute(f"""
                SELECT symbol, AVG(amount) as avg_amt
                FROM daily
                WHERE date >= date('now', '-{stock_window} days')
                GROUP BY symbol
                HAVING COUNT(*) >= {min_days}
                ORDER BY avg_amt DESC
                LIMIT ?
            """, (n_symbols,)).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT symbol, AVG(amount) as avg_amt
                FROM daily
                WHERE date >= date('now', '-{stock_window} days')
                GROUP BY symbol
                HAVING COUNT(*) >= {min_days}
                ORDER BY avg_amt DESC
            """).fetchall()
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
    if factor_names is None:
        factor_names = get_factor_names()  # 默认: status='active'
    # else: 使用调用方传入的因子列表 (评估管道传全量)
    factor_values_by_date = {name: {} for name in factor_names}

    # 只计算最近 lookback 个交易日
    eval_dates = dates[-lookback:]

    try:
        from tqdm import tqdm
        date_iter = tqdm(eval_dates, desc="Computing factors")
    except ImportError:
        logger.info("tqdm not installed, computing without progress bar")
        date_iter = eval_dates

    # Pre-load fundamentals + financials for all eval dates (eliminates per-date DB queries)
    preloaded_financials = {}
    preloaded_fundamentals = {}
    from factor.compute import _FIN_FACTORS, _FUNDAMENTAL_FN_MAP
    need_fund = any(n in _FUNDAMENTAL_FN_MAP or n in _FIN_FACTORS for n in factor_names)
    if need_fund:
        for d in eval_dates:
            ds = d.strftime("%Y-%m-%d") if hasattr(d, 'strftime') else str(d)[:10]
            try:
                fund = store.get_fundamentals(symbols, date=ds)
                if fund is not None and not fund.empty:
                    preloaded_fundamentals[ds] = fund
            except Exception:
                continue
        logger.info(f"preloaded fundamentals for {len(preloaded_fundamentals)} dates")
        # Financials preload (subset of dates that have fundamentals)
        if any(n in _FIN_FACTORS for n in factor_names):
            for d in eval_dates:
                ds = d.strftime("%Y-%m-%d") if hasattr(d, 'strftime') else str(d)[:10]
                try:
                    fin = store.get_financials(symbols, date=ds)
                    if fin is not None and not fin.empty:
                        preloaded_financials[ds] = fin
                except Exception:
                    continue
            logger.info(f"preloaded financials for {len(preloaded_financials)} dates")

    for d in date_iter:
        date_str = d.strftime("%Y-%m-%d") if hasattr(d, 'strftime') else str(d)[:10]
        # Use preloaded fundamentals when available (avoids per-date DB query for 5493 stocks)
        fundamentals = preloaded_fundamentals.get(date_str)
        if fundamentals is None:
            try:
                fundamentals = store.get_fundamentals(symbols, date=date_str)
            except Exception:
                fundamentals = None
        try:
            fv = compute_all_factors(data, date_str, fundamentals=fundamentals,
                                     factor_names=factor_names, preloaded_financials=preloaded_financials)
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
        if len(fv_dict) < _require_cfg("factor.stats.ic_min_periods"):
            continue

        # 逐截面 Rank IC
        ics = []
        ic_by_date = {}
        for date_str, fv_series in fv_dict.items():
            if date_str not in forward_1d.index:
                continue
            fr = forward_1d.loc[date_str].dropna()
            if isinstance(fr, pd.DataFrame):
                fr = fr.iloc[0]  # 取第一个symbol的返回
            common = fv_series.dropna().index.intersection(fr.dropna().index)
            if len(common) < 30:
                continue
            # Skip constant arrays (e.g., analyst_buy all-zero)
            if np.std(fv_series.loc[common]) == 0 or np.std(fr.loc[common]) == 0:
                continue
            from scipy import stats
            rho, _ = stats.spearmanr(fv_series.loc[common], fr.loc[common])
            if not np.isnan(rho):
                ics.append(rho)
                ic_by_date[date_str] = float(rho)

        ic_series[name] = ic_by_date

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
                # Skip constant arrays
                if np.std(fv_series.loc[common]) == 0 or np.std(fr.loc[common]) == 0:
                    continue
                from scipy import stats
                rho, _ = stats.spearmanr(fv_series.loc[common], fr.loc[common])
                if not np.isnan(rho):
                    h_ics.append(rho)
            decay[horizon] = round(float(np.mean(h_ics)), 4) if h_ics else 0.0
        ic_decay[name] = decay

    # 6. 计算因子相关性矩阵 (pairwise — 不要求所有因子同日期都有值)
    n = len(factor_names)
    corr_matrix = np.eye(n)
    corr_counts = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            ni = factor_names[i]
            nj = factor_names[j]
            common_d = set(factor_values_by_date[ni].keys()) & set(factor_values_by_date[nj].keys())
            pair_corrs = []
            for d in sorted(common_d):
                si = factor_values_by_date[ni][d].dropna()
                sj = factor_values_by_date[nj][d].dropna()
                common_sym = si.index.intersection(sj.index)
                if len(common_sym) < 30:
                    continue
                rho = si.loc[common_sym].corr(sj.loc[common_sym], method="spearman")
                if not np.isnan(rho):
                    pair_corrs.append(rho)
            if pair_corrs:
                avg = np.mean(pair_corrs)
                corr_matrix[i][j] = avg
                corr_matrix[j][i] = avg
                corr_counts[i][j] = len(pair_corrs)
                corr_counts[j][i] = len(pair_corrs)
    logger.info(f"corr matrix: {n}x{n}, avg pairwise periods: {corr_counts.sum()/(n*(n-1)):.1f}" if n > 1 else "corr: single factor")

    store.close()

    # 7. 生成因子元信息
    display_names = {
        "size": "规模",
        "momentum_63d": "动量63d",
        "momentum_126d": "动量126d",
        "momentum_252d": "动量252d",
        "volatility_126d": "波动率126d",
        "idio_vol_126d": "特质波动126d",
        "skewness_60d": "偏度60d",
        "amihud_250d": "Amihud 250d",
        "bp_ratio": "BP比率",
        "roe_ratio": "ROE比率",
        "gap_5d": "隔夜缺口 5d",
        "reversal_5d": "反转 5d",
        "turnover_rev_5d": "换手率反转 5d",
    }
    categories = {
        "size": "规模",
        "momentum_63d": "动量",
        "momentum_126d": "动量",
        "momentum_252d": "动量",
        "volatility_126d": "低波动",
        "idio_vol_126d": "特质波动",
        "skewness_60d": "偏度",
        "amihud_250d": "流动性",
        "bp_ratio": "价值",
        "roe_ratio": "盈利",
        "gap_5d": "隔夜",
        "reversal_5d": "反转",
        "turnover_rev_5d": "换手率",
    }
    sources = {
        "size": "Fama & French (1993)",
        "momentum_63d": "Jegadeesh & Titman (1993)",
        "momentum_126d": "Jegadeesh & Titman (1993)",
        "momentum_252d": "Jegadeesh & Titman (1993)",
        "volatility_126d": "Kakushadze & Serur Ch.3.4 (2018)",
        "idio_vol_126d": "Ang et al. (2006)",
        "skewness_60d": "Barberis & Huang (2008)",
        "amihud_250d": "Amihud (2002)",
        "bp_ratio": "Fama & French (1992)",
        "roe_ratio": "Fama & French (2015)",
        "gap_5d": "A股 T+1 独有异象",
        "reversal_5d": "Lehmann (1990) / Jegadeesh (1990)",
        "turnover_rev_5d": "Lee & Swaminathan (2000)",
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
        "ic_series": {
            n: ic_series.get(n, {}) for n in factor_names
        },
        "decay": {
            meta[n]["display"]: [
                ic_decay.get(n, {}).get("1d", 0.0),
                ic_decay.get(n, {}).get("5d", 0.0),
                ic_decay.get(n, {}).get("20d", 0.0),
            ]
            for n in factor_names
        },
        "corr": np.nan_to_num(corr_matrix, nan=0.0).round(4).tolist(),
        "meta": meta,
        "cached_at": datetime.now().isoformat(),
    }
    # 每次计算后同步写入 factor_registry，防止不同步
    try:
        from factor.compute import update_factor_evaluation
        for k, ic_val in ic_means.items():
            ir_val = ic_irs.get(k, 0.0)
            update_factor_evaluation(k, ic_val, ir_val)
    except Exception as e:
        logger.warning(f"factor_registry update failed: {e}")

    return result


def _empty_result() -> dict:
    """返回空结果（数据不足时）。"""
    from factor.compute import get_factor_names
    names = get_factor_names(status_filter=None)  # P45: 加载全量 IC 缓存
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


def get_cached_factor_stats(force_refresh: bool = False, n_symbols: int = None) -> dict:
    """获取缓存的因子评估数据。从 factor_snapshot 表读取，24h 过期自动重算。

    返回: compute_factor_stats() 的输出格式
    """
    if n_symbols is None:
        n_symbols = _require_cfg("factor.evaluation.n_symbols")
    import sqlite3 as _sql
    if not force_refresh:
        try:
            conn = _sql.connect(_DB_PATH)
            row = conn.execute(
                "SELECT data, created_at FROM factor_snapshot WHERE id=1"
            ).fetchone()
            conn.close()
            if row:
                cached = json.loads(row[0])
                cached_at = datetime.fromisoformat(cached.get("cached_at", "2000-01-01"))
                age_sec = (datetime.now() - cached_at).total_seconds()
                if age_sec < _SNAPSHOT_TTL_SEC:
                    logger.info(f"factor snapshot hit, age={age_sec/60:.0f}min")
                    return cached
                logger.info(f"factor snapshot expired, age={age_sec/3600:.1f}h")
        except Exception as e:
            logger.warning(f"Factor snapshot read failed: {e}")

    # 重新计算
    logger.info("computing factor stats (this may take ~30s)...")
    lookback_val = _require_cfg("factor.evaluation.lookback")
    stats = compute_factor_stats(n_symbols=n_symbols, lookback=lookback_val)

    # 存入 factor_snapshot 表 + 同步更新 factor_registry
    try:
        conn = _sql.connect(_DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO factor_snapshot (id, data, created_at, n_symbols, lookback) VALUES (1,?,datetime('now','localtime'),?,?)",
            (json.dumps(stats, ensure_ascii=False), n_symbols, lookback_val)
        )
        conn.commit()
        conn.close()
        logger.info("factor snapshot saved to factor_snapshot table")


    except Exception as e:
        logger.warning(f"Factor snapshot write failed: {e}")

    return stats



def _load_ic_from_db(filter_names=None) -> dict:
    """从 factor_registry 表加载 active 因子的 IC 权重。
    
    filter_names: 可选, 只保留这些因子名的 IC 权重 (filter_names 来自 factor_values).
    """
    try:
        import sqlite3 as _sql
        db = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
        conn = _sql.connect(db, timeout=30)
        rows = conn.execute(
            "SELECT name, ic_mean FROM factor_registry WHERE status='active'"
        ).fetchall()
        conn.close()
        if not rows:
            return {}
        ic_map = {}
        for name, ic in rows:
            ic_map[name] = ic if isinstance(ic, (int, float)) else 0.0
        if filter_names and ic_map:
            ic_map = {k: v for k, v in ic_map.items() if k in filter_names}
        total = sum(abs(v) for v in ic_map.values())
        if total > 0:
            ic_map = {k: v / total for k, v in ic_map.items()}
        logger.info(f"IC weights loaded from DB: {len(ic_map)} factors")
        return ic_map
    except Exception as e:
        logger.warning(f"factor_registry load failed: {e}")
        return {}

def load_ic_map_from_cache(factor_values: dict = None) -> dict:
    """从 factor_registry 表加载 IC 权重（单一数据源，不再依赖 JSON 文件）。

    返回: {factor_name: weight} 字典，已归一化。
    factor_values: 可选，用于过滤只保留实际计算出的因子。
    """
    return _load_ic_from_db(factor_values)


def force_refresh_cache(n_symbols: int = None) -> dict:
    """强制刷新因子评估 — 重新计算并存入 factor_snapshot 表。

    用于: 基本面数据更新后、因子变更后、每日定时任务。

    返回: compute_factor_stats() 的输出 dict。
    """
    if n_symbols is None:
        n_symbols = _require_cfg("factor.evaluation.n_symbols")
    logger.info(f"Refreshing factor stats with {n_symbols} stocks...")
    stats = get_cached_factor_stats(force_refresh=True, n_symbols=n_symbols)
    logger.info(f"Factor refresh complete: {len(stats.get('factors', []))} factors")
    return stats
