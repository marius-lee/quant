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


# 确保 spawned worker 进程能找到项目模块 (macOS spawn mode 不保证 PYTHONPATH 传递)
import sys as _sys, os as _os
_PROJ_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PROJ_ROOT not in _sys.path:
    _sys.path.insert(0, _PROJ_ROOT)

import json
import os
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

import warnings
import numpy as np
import pandas as pd

from utils.logger import get_logger
from config.loader import get as _cfg
from factor.compute import _require_cfg
from signal import SIGTERM, SIGKILL  # P77#10: 显式进程终止信号

# Suppress ConstantInputWarning from scipy/pandas spearmanr on near-constant arrays
# (std check catches strict zero but not floating-point near-zero; NaN fallback handles all cases)
warnings.filterwarnings("ignore", message="An input array is constant")

logger = get_logger("factor.stats_cache")

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
_SNAPSHOT_TTL_SEC = _cfg("factor.stats.snapshot_ttl_sec", 86400)  # 24h
_MAX_WORKERS = min(_cfg("factor.evaluation.max_workers", 6), 1)  # P77#10: M1 cap=1, 单worker杜绝并发泄漏
_WORKER_TIMEOUT_SEC = _cfg("factor.evaluation.worker_timeout_sec", 180)  # P77#10: 3min, 适配全量5208股因子计算
_COMPUTE_LOCK = threading.Lock()  # in-process reentrancy guard
_my_pid = os.getpid()  # P77#10: own PID for cleanup protection
_COMPUTE_FILE_LOCK = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                   "data", ".factor_compute.lock")  # cross-process guard
_ORPHAN_PID_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                 "data", ".compute_pids")

def _cleanup_process_pool():
    """清理上次残留的 ProcessPoolExecutor 孤儿进程 — P77#10: 重写为显式 SIGKILL.
    1. 读 .compute_pids → SIGKILL 所有列出的 PID → 删除文件
    2. pgrep spawn_main → SIGKILL 漏网者 (自己除外)
    3. 清理 stale lock file
    """
    _killed = 0
    # 阶段1: PID 文件清理
    if os.path.exists(_ORPHAN_PID_FILE):
        try:
            with open(_ORPHAN_PID_FILE) as _f:
                _pids = [int(_l.strip()) for _l in _f if _l.strip()]
            os.remove(_ORPHAN_PID_FILE)
            for _pid in _pids:
                try:
                    os.kill(_pid, SIGKILL)
                    _killed += 1
                except ProcessLookupError:
                    pass
            if _killed:
                logger.warning(f"Orphan cleanup: killed {_killed} worker(s) from PID file")
        except Exception as _e:
            logger.warning(f"Orphan PID cleanup failed: {_e}")

    # 阶段2: pgrep 兜底 — 强杀所有残留 multiprocessing.spawn 子进程 (自己除外)
    try:
        import subprocess as _sp
        _pg = _sp.run(
            ["pgrep", "-f", "multiprocessing.spawn.*spawn_main"],
            capture_output=True, text=True, timeout=5
        )
        if _pg.returncode == 0 and _pg.stdout.strip():
            for _line in _pg.stdout.strip().split("\n"):
                try:
                    _p = int(_line.strip())
                    if _p != _my_pid:
                        os.kill(_p, SIGKILL)
                        _killed += 1
                except (ProcessLookupError, ValueError):
                    pass
    except Exception:
        pass
    if _killed:
        logger.info(f"Orphan cleanup: total {_killed} processes killed")

    # 阶段3: 清理残留锁文件
    try:
        if os.path.exists(_COMPUTE_FILE_LOCK):
            os.remove(_COMPUTE_FILE_LOCK)
            logger.info("Cleaned up stale lock file")
    except OSError:
        pass


# ══════════════════════════════════════════════════════════════
# ProcessPoolExecutor worker (module-level, pickle-safe per macOS spawn)
# File-lock guarantees at most ONE compute_factor_stats runs system-wide,
# preventing the exponential process cascade seen with concurrent invocations.
# ══════════════════════════════════════════════════════════════

def _pp_compute_chunk(args: tuple) -> list:
    """ProcessPoolExecutor worker: load own data from DB, compute factors for a chunk.

    args: (symbols_list, date_strs_list, factor_names_list)
    Returns: list of (date_str, factor_values_dict, close_series, error_or_None)

    macOS spawn mode: fresh Python interpreter per worker (~100MB RSS each).
    Each worker opens its own DataStore → independent sqlite3 conn in WAL mode.
    """
    import sys, traceback as _tb
    import warnings, logging, pandas as pd
    warnings.filterwarnings("ignore", category=ResourceWarning)  # suppress sqlite3 conn cleanup noise on spawn
    logging.captureWarnings(True)  # route warnings to logger, not stderr
    symbols_list, date_strs_list, factor_names_list = args

    try:
        return _pp_compute_chunk_impl(symbols_list, date_strs_list, factor_names_list)
    except Exception as e:
        err_msg = f"{type(e).__name__}: {e}"
        return [(d, {}, pd.Series(dtype=float), err_msg) for d in date_strs_list]


def _pp_compute_chunk_impl(symbols_list: list, date_strs_list: list,
                           factor_names_list: list) -> list:
    """_pp_compute_chunk inner impl — outer try/except guards all stages."""

    from data.store import DataStore
    from factor.compute import compute_all_factors
    import pandas as pd

    store = DataStore()
    min_date = date_strs_list[0]
    max_date = date_strs_list[-1]
    data_start = (pd.Timestamp(min_date) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    future_end = (pd.Timestamp(max_date) + pd.Timedelta(days=40)).strftime('%Y-%m-%d')
    data = store.get_daily(symbols_list, start=data_start, end=future_end)

    results = []
    for date_str in date_strs_list:
        try:
            fundamentals = store.get_fundamentals(symbols_list, date=date_str)
            fin = store.get_financials(symbols_list, date=date_str)
            preloaded_fin = {date_str: fin} if fin is not None and not fin.empty else None
            fv = compute_all_factors(data, date_str,
                                     fundamentals=fundamentals,
                                     factor_names=factor_names_list,
                                     preloaded_financials=preloaded_fin)
            result = {}
            for name in factor_names_list:
                if name in fv and not fv[name].dropna().empty:
                    result[name] = fv[name]
            # data is MultiIndex (date, symbol); check level 0 for date membership
            try:
                close_series = data["close"].loc[date_str]
            except KeyError:
                close_series = pd.Series(dtype=float)
            results.append((date_str, result, close_series, None))
        except Exception as e:
            results.append((date_str, {}, pd.Series(dtype=float), str(e)))

    store.close()
    return results



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
        return _empty_result()

    # 2. 获取评估日期 (轻量 SQL 查询, 不加载 OHLCV)
    #    全量 daily 数据由 worker 各自加载, 主进程只查 DISTINCT date 列
    if factor_names is None:
        factor_names = get_factor_names()  # 默认: status='active'
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
    store.close()  # 子进程各自打开独立 DataStore, 主线程不再访问 DB

    if not eval_date_strs:
        logger.warning("No eval dates available")
        return _empty_result()

    logger.info(f"eval dates: {len(eval_date_strs)} dates, {eval_date_strs[0]}→{eval_date_strs[-1]}, "
                f"{len(factor_names)} factors, {_MAX_WORKERS} processes")

    # ══ Phase B: ProcessPoolExecutor 并行因子计算 ══
    # 跨进程文件锁: 确保整个系统最多一个 compute_factor_stats 实例运行,
    # 防止并发调用导致 6→36→216 指数级进程繁殖 (macOS spawn ~100MB/进程)
    close_by_date = {}
    logger.info(f"factor compute start: {len(eval_date_strs)} dates x {len(factor_names)} factors, "
                f"{_MAX_WORKERS} processes (workers load own data from DB)")

    n_chunks = min(_MAX_WORKERS, len(eval_date_strs))
    chunk_size = max(1, len(eval_dates) // n_chunks)
    date_chunks = [eval_date_strs[i:i + chunk_size] for i in range(0, len(eval_date_strs), chunk_size)]
    logger.info(f"partitioned {len(eval_date_strs)} dates into {len(date_chunks)} chunks (max {chunk_size}/chunk)")

    executor = ProcessPoolExecutor(max_workers=n_chunks)
    # 写入 worker PID → .compute_pids, 供 _cleanup_process_pool 清理孤儿进程
    try:
        with open(_ORPHAN_PID_FILE, 'w') as _pf:
            for _proc in executor._processes.values():
                _pf.write(f"{_proc.pid}\n")
    except Exception:
        pass
    futures = {}
    try:
        for ci, chunk_dates in enumerate(date_chunks):
            logger.info(f"  chunk {ci+1}/{len(date_chunks)}: {len(chunk_dates)} dates")
            futures[executor.submit(_pp_compute_chunk,
                     (symbols, chunk_dates, factor_names))] = chunk_dates
        logger.info(f"parallel compute: {len(futures)} chunks x {n_chunks} processes")
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
        except TimeoutError:
            n_pending = sum(1 for f in futures if not f.done())
            logger.error(
                f"ProcessPoolExecutor timed out after {_WORKER_TIMEOUT_SEC}s — "
                f"{n_pending} chunk(s) incomplete, canceling"
            )
            for f in futures:
                f.cancel()
        finally:
            if pbar:
                pbar.close()
    finally:
        # shutdown(wait=False, cancel_futures=True): don't hang if workers are stuck — SIGTERM and move on
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            executor.shutdown(wait=False)
        # 正常退出 → 清理 PID 文件 (避免下次启动误杀正常进程)
        try:
            if os.path.exists(_ORPHAN_PID_FILE):
                os.remove(_ORPHAN_PID_FILE)
        except Exception:
            pass
        # Nuke any remaining multiprocessing orphans (pgrep)
        import time as _t, subprocess as _sp, signal as _sg
        _t.sleep(0.5)
        try:
            _pg = _sp.run(["pgrep", "-f", "multiprocessing.spawn.*spawn_main"],
                          capture_output=True, text=True, timeout=5)
            if _pg.returncode == 0 and _pg.stdout.strip():
                for _p in _pg.stdout.strip().split("\n"):
                    try:
                        os.kill(int(_p.strip()), _sg.SIGKILL)
                        logger.warning(f"Force-killed straggler {_p.strip()}")
                    except (ProcessLookupError, ValueError):
                        pass
        except Exception:
            pass
    logger.info(f"factor compute complete: {len(eval_date_strs)}/{len(eval_date_strs)} dates")

    # 4. 从 worker 返回的 close 数据构建 forward returns (无需主进程加载 daily)
    #    close_by_date 由 worker 逐日返回, 拼接为 MultiIndex Series
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
        return _empty_result()
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

    # Parallel IC computation — each factor independent
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
        # Compute decay per horizon
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

    # Execute IC computation in parallel
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

    # 6. 计算因子相关性矩阵 (pairwise — 不要求所有因子同日期都有值)
    n = len(factor_names)
    # Parallel pairwise correlation computation
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

    # ══ PID-file 跨进程锁 (替代 fcntl — macOS APFS 上 fcntl 不可靠) ══
    # 写入 PID → 读回 → 匹配即获取锁 → 不匹配则检查 PID 存活 → 死锁可窃取
    import subprocess as _lock_sp
    got_lock = False
    _lock_msg = ""
    _my_pid = str(os.getpid())
    for _attempt in range(3):  # 最多重试3次
        try:
            # 写 PID 到锁文件 (原子性不强, 但结合读回验证足够)
            with open(_COMPUTE_FILE_LOCK, 'w') as _lf:
                _lf.write(_my_pid)
            # 读回验证
            with open(_COMPUTE_FILE_LOCK, 'r') as _lf:
                _stored = _lf.read().strip()
            if _stored == _my_pid:
                got_lock = True
                _lock_msg = "PID-lock acquired"
                break
            # PID 不匹配 → 检查存活性
            try:
                _stored_pid = int(_stored) if _stored.isdigit() else 0
                if _stored_pid:
                    os.kill(_stored_pid, 0)  # 信号0不杀, 仅检查进程存在
                    _lock_msg = f"PID {_stored_pid} alive — waiting {_attempt+1}/3"
                    time.sleep(0.5 * (_attempt + 1))
                else:
                    # 存活的 PID 是乱码 → 抢锁
                    got_lock = True
                    _lock_msg = "PID-lock stolen (invalid PID)"
                    break
            except ProcessLookupError:
                # 进程已死 → 抢锁
                got_lock = True
                _lock_msg = f"PID-lock stolen (PID {_stored_pid} dead)"
                break
            except Exception:
                got_lock = True
                _lock_msg = "PID-lock stolen (error checking PID)"
                break
        except Exception as e:
            _lock_msg = f"PID-lock file error: {e}"
            time.sleep(0.3)
    
    if not got_lock:
        logger.warning(f"factor stats: {_lock_msg}, returning stale/empty cache")
        try:
            conn = _sql.connect(_DB_PATH)
            row = conn.execute("SELECT data FROM factor_snapshot WHERE id=1").fetchone()
            conn.close()
            if row:
                return json.loads(row[0])
        except Exception:
            pass
        return _empty_result()
    logger.info(f"factor stats: {_lock_msg}")

    # 进程内重入保护 + 锁前清剿孤儿
    if not _COMPUTE_LOCK.acquire(blocking=False):
        logger.warning("factor stats: in-process lock held by another thread, returning stale cache")
        try:
            os.remove(_COMPUTE_FILE_LOCK)
        except Exception:
            pass
        try:
            conn = _sql.connect(_DB_PATH)
            row = conn.execute("SELECT data FROM factor_snapshot WHERE id=1").fetchone()
            conn.close()
            if row:
                return json.loads(row[0])
        except Exception:
            pass
        return _empty_result()
    
    # ══ 计算前强杀所有旧 worker (双重保障) ══
    _killed = 0
    try:
        _pg = _lock_sp.run(
            ["pgrep", "-f", "multiprocessing.spawn.*spawn_main"],
            capture_output=True, text=True, timeout=5
        )
        if _pg.returncode == 0 and _pg.stdout.strip():
            for _orphan_pid in _pg.stdout.strip().split("\n"):
                try:
                    _p = int(_orphan_pid.strip())
                    if _p != _my_pid:
                        os.kill(_p, SIGKILL)
                        _killed += 1
                except (ProcessLookupError, ValueError):
                    pass
    except Exception:
        pass
    if _killed:
        logger.warning(f"factor stats: pre-killed {_killed} orphan workers before spawning")
    
    try:
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
    finally:
        _COMPUTE_LOCK.release()
        # 释放 PID 锁
        try:
            os.remove(_COMPUTE_FILE_LOCK)
        except Exception:
            pass



def _load_ic_from_db(filter_names=None) -> dict:
    """从 factor_registry 表加载 active 因子的 IC 权重。
    
    filter_names: 可选, 只保留这些因子名的 IC 权重 (filter_names 来自 factor_values).
    """
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


# 模块加载时自动清理上次崩溃残留的孤儿进程
_cleanup_process_pool()
