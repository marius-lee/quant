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

多线程策略 (P78): 因子计算使用 ThreadPoolExecutor，worker 线程各自打开 DataStore
(sqlite3 WAL 模式支持多线程并发读)。线程随 with 语句自动回收，无孤儿进程风险。
"""

import json
import os
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

import warnings
import numpy as np
import pandas as pd

from utils.logger import get_logger
from config.constants import _require_cfg

# Suppress ConstantInputWarning from scipy/pandas spearmanr on near-constant arrays
warnings.filterwarnings("ignore", message="An input array is constant")

logger = get_logger("factor.stats_cache")

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
_SNAPSHOT_TTL_SEC = _require_cfg("factor.stats.snapshot_ttl_sec")
_MAX_WORKERS = _require_cfg("factor.evaluation.max_workers")
_WORKER_TIMEOUT_SEC = _require_cfg("factor.evaluation.worker_timeout_sec")
_COMPUTE_LOCK = threading.Lock()  # in-process reentrancy guard: 因子计算最多一个线程运行


def compute_factor_stats(
    symbols: list = None, n_symbols: int = None, lookback: int = None,
    factor_names: list = None,
) -> dict:
    """计算所有已注册因子的评估统计量，返回前端可用格式。

    n_symbols / lookback 默认值来源: config.yaml factor.evaluation (单一真相源).
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
        stock_window = int(lookback * 1.5)
        min_days = max(5, lookback // 2)
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
        return _empty_result(factor_names)

    # 2. 获取评估日期
    if factor_names is None:
        factor_names = get_factor_names()
    factor_values_by_date = {name: {} for name in factor_names}

    conn = store._connect()
    end_date = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - pd.Timedelta(days=lookback * 1.5)).strftime("%Y-%m-%d")
    eval_dates_raw = conn.execute(
        "SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date)
    ).fetchall()
    eval_dates = [pd.Timestamp(r[0]) for r in eval_dates_raw][-lookback:]
    eval_date_strs = [d.strftime("%Y-%m-%d") for d in eval_dates]
    store.close()

    if not eval_date_strs:
        logger.warning("No eval dates available")
        return _empty_result(factor_names)

    logger.info(f"eval dates: {len(eval_date_strs)} dates, {eval_date_strs[0]}→{eval_date_strs[-1]}, "
                f"{len(factor_names)} factors, {_MAX_WORKERS} threads")

    # ══ Phase B: ThreadPoolExecutor 并行因子计算 (P78) ══
    # 每个线程打开独立 DataStore, sqlite3 WAL mode 支持多线程并发读
    close_by_date = {}
    logger.info(f"factor compute start: {len(eval_date_strs)} dates x {len(factor_names)} factors, "
                f"{_MAX_WORKERS} threads (each loads own DataStore)")

    def _thread_compute_chunk(chunk_dates: list) -> list:
        """Thread worker: each thread opens its own DataStore, loads data, computes factors."""
        import logging as _log
        _log.captureWarnings(True)
        try:
            from data.store import DataStore
            from factor.compute import compute_all_factors
            import pandas as _pd

            _store = DataStore()
            min_date = chunk_dates[0]
            max_date = chunk_dates[-1]
            data_start = (_pd.Timestamp(min_date) - _pd.Timedelta(days=365)).strftime("%Y-%m-%d")
            future_end = (_pd.Timestamp(max_date) + _pd.Timedelta(days=40)).strftime("%Y-%m-%d")
            data = _store.get_daily(symbols, start=data_start, end=future_end)

            results = []
            for date_str in chunk_dates:
                try:
                    fundamentals = _store.get_fundamentals(symbols, date=date_str)
                    fin = _store.get_financials(symbols, date=date_str)
                    preloaded_fin = {date_str: fin} if fin is not None and not fin.empty else None
                    fv = compute_all_factors(data, date_str,
                                             fundamentals=fundamentals,
                                             factor_names=factor_names,
                                             preloaded_financials=preloaded_fin)
                    result = {}
                    for name in factor_names:
                        if name in fv and not fv[name].dropna().empty:
                            result[name] = fv[name]
                    try:
                        close_series = data["close"].loc[date_str]
                    except KeyError:
                        close_series = _pd.Series(dtype=float)
                    results.append((date_str, result, close_series, None))
                except Exception as e:
                    results.append((date_str, {}, _pd.Series(dtype=float), str(e)))

            _store.close()
            return results
        except Exception as e:
            logger.exception(f"Thread worker fatal error: {type(e).__name__}: {e}")
            return [(d, {}, _pd.Series(dtype=float), f"{type(e).__name__}: {e}") for d in chunk_dates]

    n_chunks = min(_MAX_WORKERS, len(eval_date_strs))
    chunk_size = max(1, len(eval_date_strs) // n_chunks)
    date_chunks = [eval_date_strs[i:i + chunk_size] for i in range(0, len(eval_date_strs), chunk_size)]
    logger.info(f"partitioned {len(eval_date_strs)} dates into {len(date_chunks)} chunks (max {chunk_size}/chunk)")

    with ThreadPoolExecutor(max_workers=n_chunks) as executor:
        futures = {executor.submit(_thread_compute_chunk, chunk_dates): ci
                   for ci, chunk_dates in enumerate(date_chunks)}
        for ci, chunk_dates in enumerate(date_chunks):
            logger.info(f"  chunk {ci+1}/{len(date_chunks)}: {len(chunk_dates)} dates")

        logger.info(f"parallel compute: {len(futures)} chunks x {n_chunks} threads")

        try:
            from tqdm import tqdm
            pbar = tqdm(total=len(futures), desc="Computing factors (parallel)")
        except ImportError:
            pbar = None

        try:
            for future in as_completed(futures, timeout=_WORKER_TIMEOUT_SEC):
                chunk_results = future.result()
                for date_str, fv_partial, close_series, err in chunk_results:
                    if err:
                        logger.warning(f"Factor compute failed at {date_str}: {err}")
                    else:
                        for name, series in fv_partial.items():
                            factor_values_by_date[name][date_str] = series
                        if close_series is not None and not close_series.empty:
                            close_by_date[date_str] = close_series
                if pbar:
                    pbar.update(1)
        except FuturesTimeoutError:
            n_pending = sum(1 for f in futures if not f.done())
            logger.error(
                f"ThreadPoolExecutor timed out after {_WORKER_TIMEOUT_SEC}s — "
                f"{n_pending} chunk(s) incomplete, canceling"
            )
            for f in futures:
                f.cancel()
        finally:
            if pbar:
                pbar.close()

    logger.info(f"factor compute complete: {len(eval_date_strs)}/{len(eval_date_strs)} dates")

    # 4. 构建 forward returns
    close_parts = []
    for date_str in sorted(close_by_date.keys()):
        s = close_by_date[date_str]
        if s.empty:
            continue
        mi = pd.MultiIndex.from_tuples([(date_str, sym) for sym in s.index],
                                        names=['date', 'symbol'])
        close_parts.append(pd.Series(s.values, index=mi, name='close'))
    if not close_parts:
        logger.warning("No close data from workers — cannot compute forward returns")
        return _empty_result(factor_names)
    close = pd.concat(close_parts)
    if isinstance(close, pd.Series):
        close = close.unstack()
    forward_1d = close.pct_change().shift(-1)
    forward_5d = close.pct_change(5).shift(-5)
    forward_20d = close.pct_change(20).shift(-20)

    # 5. 计算每个因子的 IC/IR
    ic_means = {}
    ic_irs = {}
    ic_series = {}
    ic_decay = {}

    min_periods = _require_cfg("factor.stats.ic_min_periods")

    def _compute_ic(name, fv_dict, forward_1d, forward_5d, forward_20d):
        if len(fv_dict) < min_periods:
            return name, None
        from scipy import stats as _stats
        ics = []
        ic_by_date = {}
        for date_str, fv_series in fv_dict.items():
            if date_str not in forward_1d.index:
                continue
            fr = forward_1d.loc[date_str].dropna()
            if isinstance(fr, pd.DataFrame):
                fr = fr.iloc[0]
            common = fv_series.dropna().index.intersection(fr.dropna().index)
            if len(common) < 30:
                continue
            if np.std(fv_series.loc[common]) < 1e-10 or np.std(fr.loc[common]) < 1e-10:
                continue
            rho, _ = _stats.spearmanr(fv_series.loc[common], fr.loc[common])
            if not np.isnan(rho):
                ics.append(rho)
                ic_by_date[date_str] = float(rho)
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
                if np.std(fv_series.loc[common]) < 1e-10 or np.std(fr.loc[common]) < 1e-10:
                    continue
                from scipy import stats
                rho, _ = stats.spearmanr(fv_series.loc[common], fr.loc[common])
                if not np.isnan(rho):
                    h_ics.append(rho)
            decay[horizon] = round(float(np.mean(h_ics)), 4) if h_ics else 0.0
        if ics:
            ic_arr = np.array(ics)
            ic_mean = float(np.mean(ic_arr))
            ic_ir_val = float(np.mean(ic_arr) / np.std(ic_arr, ddof=1)) if np.std(ic_arr, ddof=1) > 0 else 0.0
        else:
            ic_mean = 0.0
            ic_ir_val = 0.0
        return name, (ic_mean, ic_ir_val, ic_by_date, decay)

    logger.info(f"IC compute start: {len(factor_names)} factors")

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_compute_ic, name, factor_values_by_date[name],
                                   forward_1d, forward_5d, forward_20d): name
                   for name in factor_names}
        ic_completed = 0
        for future in as_completed(futures, timeout=_WORKER_TIMEOUT_SEC):
            name, result = future.result()
            if result is not None:
                ic_means[name], ic_irs[name], ic_series[name], ic_decay[name] = result
            else:
                logger.info(f"IC compute skip {name}: < min_periods")
            ic_completed += 1
            if ic_completed % 10 == 0:
                logger.info(f"IC compute progress: {ic_completed}/{len(factor_names)} factors")
        logger.info(f"IC compute complete: {len(factor_names)} factors")

    # 6. 计算因子相关性矩阵
    n = len(factor_names)

    def _compute_pair(i, j, ni, nj):
        common_d = set(factor_values_by_date[ni].keys()) & set(factor_values_by_date[nj].keys())
        pair_corrs = []
        for d in sorted(common_d):
            si = factor_values_by_date[ni][d].dropna()
            sj = factor_values_by_date[nj][d].dropna()
            common_sym = si.index.intersection(sj.index)
            if len(common_sym) < 30:
                continue
            if np.std(si.loc[common_sym]) < 1e-10 or np.std(sj.loc[common_sym]) < 1e-10:
                continue
            rho = si.loc[common_sym].corr(sj.loc[common_sym], method="spearman")
            if not np.isnan(rho):
                pair_corrs.append(rho)
        avg = float(np.mean(pair_corrs)) if pair_corrs else 0.0
        return i, j, avg, len(pair_corrs)

    pairs = [(i, j, factor_names[i], factor_names[j])
             for i in range(n) for j in range(i + 1, n)]
    logger.info(f"correlation matrix: {n}×{n} factors, {len(pairs)} pairwise pairs")
    corr_matrix = np.eye(n)
    corr_counts = np.zeros((n, n))
    if pairs:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
            futures = {executor.submit(_compute_pair, i, j, ni, nj): (i, j)
                       for i, j, ni, nj in pairs}
            for future in as_completed(futures, timeout=_WORKER_TIMEOUT_SEC):
                i, j, avg, n_pairs = future.result()
                corr_matrix[i][j] = avg
                corr_matrix[j][i] = avg
                corr_counts[i][j] = n_pairs
                corr_counts[j][i] = n_pairs
    logger.info(f"corr matrix: {n}x{n}, avg pairwise periods: {corr_counts.sum()/(n*(n-1)):.1f}" if n > 1 else "corr: single factor")

    # 7. 生成因子元信息
    display_names = {
        "size": "规模", "momentum_63d": "动量63d", "momentum_126d": "动量126d",
        "momentum_252d": "动量252d", "volatility_126d": "波动率126d",
        "idio_vol_126d": "特质波动126d", "skewness_60d": "偏度60d",
        "amihud_250d": "Amihud 250d", "bp_ratio": "BP比率",
        "roe_ratio": "ROE比率", "gap_5d": "隔夜缺口 5d",
        "reversal_5d": "反转 5d", "turnover_rev_5d": "换手率反转 5d",
    }
    categories = {
        "size": "规模", "momentum_63d": "动量", "momentum_126d": "动量",
        "momentum_252d": "动量", "volatility_126d": "低波动",
        "idio_vol_126d": "特质波动", "skewness_60d": "偏度",
        "amihud_250d": "流动性", "bp_ratio": "价值", "roe_ratio": "盈利",
        "gap_5d": "隔夜", "reversal_5d": "反转", "turnover_rev_5d": "换手率",
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
    # 同步写入 factor_registry
    try:
        from factor.compute import update_factor_evaluation
        for k, ic_val in ic_means.items():
            ir_val = ic_irs.get(k, 0.0)
            update_factor_evaluation(k, ic_val, ir_val)
    except Exception as e:
        logger.warning(f"factor_registry update failed: {e}")

    return result


def _empty_result(factor_names: list = None) -> dict:
    """返回空结果（数据不足时）。使用传入 factor_names，None 时回退到全量因子。"""
    if factor_names is None:
        from factor.compute import get_factor_names
        factor_names = get_factor_names(status_filter=None)
    names = factor_names
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

    P78: 纯线程模型 — ThreadPoolExecutor with 语句自动回收，零孤儿进程风险。
    _COMPUTE_LOCK (threading.Lock) 防并发重入：最多一个线程进入计算路径。

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

    # 进程内重入保护
    if not _COMPUTE_LOCK.acquire(blocking=False):
        logger.warning("factor stats: in-process lock held by another thread, returning stale cache")
        try:
            conn = _sql.connect(_DB_PATH)
            row = conn.execute("SELECT data FROM factor_snapshot WHERE id=1").fetchone()
            conn.close()
            if row:
                return json.loads(row[0])
        except Exception:
            pass
        return _empty_result()

    try:
        logger.info("computing factor stats (this may take ~30s)...")
        lookback_val = _require_cfg("factor.evaluation.lookback")
        stats = compute_factor_stats(n_symbols=n_symbols, lookback=lookback_val)

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
    finally:
        _COMPUTE_LOCK.release()


def _load_ic_from_db(filter_names=None) -> dict:
    """从 factor_registry 表加载 active 因子的 IC 权重。"""
    try:
        import sqlite3 as _sql
        db = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
        conn = _sql.connect(db, timeout=30)
        rows = conn.execute(
            "SELECT name, ic_mean FROM factor_registry WHERE status IN ('active','monitoring')"
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
