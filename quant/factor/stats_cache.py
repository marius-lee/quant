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

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

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
    factor_names: list = None, status_filter=None,
) -> dict:
    """计算所有已注册因子的评估统计量，返回前端可用格式。

    n_symbols / lookback 默认值来源: config.yaml factor.evaluation (单一真相源).
    """
    if n_symbols is None:
        n_symbols = _require_cfg("factor.evaluation.n_symbols")
    if lookback is None:
        lookback = _require_cfg("factor.evaluation.lookback")

    from quant.data.store import DataStore
    from quant.factor.compute import compute_all_factors, get_factor_names

    store = DataStore()

    # 1. 选择样本股票 — 统一用 UniverseRepo (survivorship-free, 与 loop.py 一致)
    if symbols is None:
        from quant.data.repos.universe_repo import UniverseRepo
        all_symbols = UniverseRepo().get_symbols(exclude_market="BJ")
        if n_symbols and n_symbols > 0 and len(all_symbols) > n_symbols:
            # 按流动性排名截断 (与 backtest/loop.py 的 rank_by_turnover 逻辑一致)
            conn = store._connect()
            stock_window = int(lookback * 1.5)
            min_days = max(5, lookback // 2)
            placeholders = ",".join("?" * len(all_symbols))
            rows = conn.execute(f"""
                SELECT symbol, AVG(amount) as avg_amt
                FROM daily
                WHERE symbol IN ({placeholders})
                  AND date >= date('now', '-{stock_window} days')
                GROUP BY symbol
                HAVING COUNT(*) >= {min_days}
                ORDER BY avg_amt DESC
                LIMIT ?
            """, all_symbols + [n_symbols]).fetchall()
            symbols = [r[0] for r in rows]
        else:
            symbols = all_symbols

    if not symbols:
        logger.warning("No symbols available for factor evaluation")
        return _empty_result(factor_names)

    # 2. 获取评估日期
    if factor_names is None:
        # 回测池 = candidate + monitoring + retired (ADR-026)
        # 排除: active(已投产, 实盘模块管理), rejected(永久淘汰)
        if status_filter is None:
            status_filter = 'backtesting'
        factor_names = get_factor_names(status_filter=status_filter)
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

    # ══ Phase B: 从 factor_cache.db 读取因子值 + IC 计算 (不再重算) ══
    from quant.factor.ic import compute_ic as _compute_ic

    # 从 factor_cache.db 加载 close 行情数据 (仅用于后续相关性矩阵计算, Phase 2 不需要)
    data_start = str(pd.Timestamp(eval_date_strs[0]) - pd.Timedelta(days=lookback))[:10]
    data_end = str(pd.Timestamp(eval_date_strs[-1]) + pd.Timedelta(days=40))[:10]
    _shared_data = store.get_daily(symbols, start=data_start, end=data_end)

    logger.info(
        f"factor_cache: computing IC for {len(factor_names)} factors "
        f"over {len(eval_date_strs)} dates ({eval_date_strs[0]}→{eval_date_strs[-1]})"
    )

    _ic_result = _compute_ic(
        factor_names=factor_names,
        date=eval_date_strs[-1],
        symbols=symbols,
        lookback=lookback,
        status_filter=None,  # 已传 factor_names, 不额外过滤
    )

    ic_means = _ic_result["ic_means"]
    ic_irs = _ic_result["ic_irs"]
    ic_series = _ic_result.get("ic_series", {})
    ic_decay = _ic_result.get("ic_decay", {})

    _n_valid = _ic_result.get("n_valid", 0)
    logger.info(f"factor_cache IC done: {_n_valid}/{len(factor_names)} factors with valid IC")

    # 从 IC 结果构建 forward returns 结构 (后续代码需要)
    import pandas as _pd
    close_parts = []
    for _ds in sorted(eval_date_strs):
        try:
            s = _shared_data["close"].loc[_ds] if _ds in _shared_data.index else _pd.Series(dtype=float)
        except Exception:
            s = _pd.Series(dtype=float)
        if s.empty:
            continue
        mi = _pd.MultiIndex.from_tuples([(_ds, sym) for sym in s.index],
                                        names=['date', 'symbol'])
        close_parts.append(_pd.Series(s.values, index=mi, name='close'))
    if not close_parts:
        logger.warning("No close data — cannot compute forward returns")
        return _empty_result(factor_names)
    close = _pd.concat(close_parts)
    if isinstance(close, _pd.Series):
        close = close.unstack()
    forward_1d = close.pct_change().shift(-1)
    forward_5d = close.pct_change(5).shift(-5)
    forward_20d = close.pct_change(20).shift(-20)

    # close_by_date: 不再需要 (后续用 forward_1d)
    close_by_date = {}
    # 从 factor_cache.db 读取因子值用于相关性矩阵计算 (修正 test-v139 引入的全零 bug)
    from quant.factor.store import FactorStore
    _fs = FactorStore()
    factor_values_by_date = {name: {} for name in factor_names}
    _fv_miss = 0  # Q7-5 fix: 加载失败计数 (原裸 except: pass 吞错)
    for _ds in eval_date_strs:
        try:
            _fv = _fs.load(_ds, symbols=symbols, factor_names=factor_names)
            for _fn, _series in _fv.items():
                if isinstance(_series, pd.Series) and _series.notna().sum() >= 30:
                    factor_values_by_date[_fn][_ds] = _series.dropna()
        except Exception as _load_err:
            _fv_miss += 1
            logger.debug(f"factor_cache: FactorStore.load({_ds}) failed ({_fv_miss} misses): {_load_err}")
    _fs.close()
    if _fv_miss:
        logger.warning(f"factor_cache: {_fv_miss}/{len(eval_date_strs)} dates failed FactorStore.load")

    logger.info(
        f"factor_cache: loaded IC data for {_n_valid} factors, "
        f"{sum(1 for v in ic_means.values() if abs(v) > 0.001)} with non-zero IC"
    )
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
        executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS)
        try:
            futures = {executor.submit(_compute_pair, i, j, ni, nj): (i, j)
                        for i, j, ni, nj in pairs}
            for future in as_completed(futures):
                i, j, avg, n_pairs = future.result()
                corr_matrix[i][j] = avg
                corr_matrix[j][i] = avg
                corr_counts[i][j] = n_pairs
                corr_counts[j][i] = n_pairs
        finally:
            executor.shutdown(wait=True)
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
        from quant.factor.compute import update_factor_evaluation
        for k, ic_val in ic_means.items():
            ir_val = ic_irs.get(k, 0.0)
            update_factor_evaluation(k, ic_val, ir_val)
    except Exception as e:
        logger.warning(f"factor_registry update failed: {e}")

    return result


def _empty_result(factor_names: list = None) -> dict:
    """返回空结果（数据不足时）。使用传入 factor_names，None 时回退到全量因子。"""
    if factor_names is None:
        from quant.factor.compute import get_factor_names
        # 回测池 = candidate + monitoring + retired (ADR-026)
        # 排除: active(已投产, 实盘模块管理), rejected(永久淘汰)
        if status_filter is None:
            status_filter = 'backtesting'
        factor_names = get_factor_names(status_filter=status_filter)
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


def get_cached_factor_stats(force_refresh: bool = False, n_symbols: int = None, status_filter=None) -> dict:
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
            import logging; logging.getLogger("quant.factor.stats_cache").warning("load_latest failed", exc_info=True)
            return _empty_result()

    try:
        logger.info("computing factor stats (this may take ~30s)...")
        lookback_val = _require_cfg("factor.evaluation.lookback")
        stats = compute_factor_stats(n_symbols=n_symbols, lookback=lookback_val, status_filter=status_filter)

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


def _load_ic_from_db(filter_names=None, scope='live') -> dict:
    """从 factor_ic_daily 表加载因子 IC 权重 (按 scope 隔离).

    scope: 'live' (实盘, 读 factor_registry.ic_mean for active+monitoring),
           'backtest' (回测, 读 factor_ic_daily scope='backtest' 的末端 IC 均值).

    注意: live scope 读 factor_registry.ic_mean (由 nightly attribution sync 写入);
          backtest scope 读 factor_ic_daily (由 compute_backtest_ic 写入).
    """
    from quant.data.repos import FactorRepo
    repo = FactorRepo()
    ic_map = {}

    if scope == 'live':
        # 实盘: 从 factor_registry 读 active+monitoring 的 ic_mean
        rows = repo.get_factors_with_ic(('active', 'probation'))
        if not rows:
            logger.warning("IC weights: no active/monitoring factors with ic_mean in factor_registry")
            return {}
        for r in rows:
            ic_map[r["name"]] = r["ic_mean"] if isinstance(r["ic_mean"], (int, float)) else 0.0
    elif scope == 'backtest':
        # 回测: 从 factor_ic_daily 读取 scope='backtest' 最近一条的 ic_value
        # 获取所有 backtesting 因子
        from quant.factor.compute._registry import get_factor_names
        bt_names = get_factor_names(status_filter='backtesting')
        if not bt_names:
            logger.warning("IC weights: no backtesting factors")
            return {}
        conn = repo._conn()
        try:
            ph = ",".join("?" * len(bt_names))
            rows = conn.execute(
                f"SELECT factor_name, ic_value FROM factor_ic_daily "
                f"WHERE scope='backtest' AND factor_name IN ({ph}) "
                f"AND date = (SELECT MAX(date) FROM factor_ic_daily WHERE scope='backtest')",
                tuple(bt_names)
            ).fetchall()
            for r in rows:
                ic_map[r[0]] = r[1] if isinstance(r[1], (int, float)) else 0.0
        finally:
            conn.close()
    else:
        raise ValueError(f"Invalid scope: {scope}")

    if filter_names and ic_map:
        ic_map = {k: v for k, v in ic_map.items() if k in filter_names}
    total = sum(abs(v) for v in ic_map.values())
    if total > 0:
        ic_map = {k: v / total for k, v in ic_map.items()}
    logger.info(f"IC weights loaded from DB: {len(ic_map)} factors (scope={scope})")
    return ic_map


def compute_backtest_ic(start_date: str, n_train_days: int = 120,
                       status_filter: str = 'backtesting') -> dict:
    """计算回测用 IC 权重 — 训练期 OOS 验证 → 写入 factor_ic_daily(scope='backtest').

    start_date: 回测开始日期 (如 '2026-01-01')
    n_train_days: 训练期天数, 从 start_date 往前数
    status_filter: 因子池 ('backtesting' = candidate+monitoring+retired, ADR-026)

    返回: {factor_name: weight} 归一化 IC 权重, 供 generate_signals(ic_map=...) 使用.
    同时写入 factor_ic_daily(scope='backtest') 持久化.
    """
    from datetime import timedelta
    import pandas as pd

    train_end = start_date
    start_dt = pd.Timestamp(start_date)
    train_start = (start_dt - timedelta(days=n_train_days)).strftime("%Y-%m-%d")
    logger.info(f"backtest IC: computing for {status_filter} pool, train window={train_start}→{train_end}")

    from quant.scheduler.oos_verify import run_oos_check
    result = run_oos_check(
        train_end,
        status_filter=status_filter,
        train_days=n_train_days,
        test_days=_require_cfg("oos_verify.test_window_days"),
        decay_warn_threshold=_require_cfg("oos_verify.decay_warn_threshold"),
        n_symbols=_require_cfg("oos_verify.backtest_n_symbols"),
    )
    if result.get("alert"):
        logger.warning(f"backtest IC: OOS decay alert for {train_end}")

    per_factor = result.get("details", {}).get("per_factor", {})
    ic_daily = result.get("ic_daily", {})

    # Write to factor_ic_daily with scope='backtest'
    from quant.data.repos import FactorRepo
    f_repo = FactorRepo()
    f_repo.ensure_ic_daily_table()
    written = 0
    for fname, daily_ics in ic_daily.items():
        for ds, ic_val in daily_ics.items():
            f_repo.insert_ic_daily(ds, fname, float(ic_val),
                                   n_stocks=len(per_factor),
                                   scope='backtest')
            written += 1
    logger.info(f"backtest IC: wrote {written} rows to factor_ic_daily(scope='backtest')")

    # Build ic_map from OOS IR (more robust than raw ic_mean for 1-day IC)
    ic_map = {}
    for fname, info in per_factor.items():
        ic_mean = float(info.get("ic_mean", 0))
        ic_ir = float(info.get("oos_ir", info.get("is_ir", 0)))
        # Q7-4 fix: 保留 ic_ir 符号 (原 abs → 负 IC 因子在 composite
        # 模式信号方向被反向); 归一化仍按 sum(abs(weight))
        weight = ic_ir
        ic_map[fname] = {
            "ic_mean": ic_mean,
            "ic_ir": ic_ir,
            "weight": weight,
        }

    # Normalize weights
    total = sum(abs(v["weight"]) for v in ic_map.values())
    if total > 0:
        for k in ic_map:
            ic_map[k]["weight"] = ic_map[k]["weight"] / total
    logger.info(f"backtest IC: {len(ic_map)} factors with weights (train_end={train_end})")
    return ic_map


def _bayesian_shrink_ic_map(ic_map: dict) -> dict:
    """ALG2: Bayesian shrinkage of IC estimates toward cross-sectional prior.

    Grinold & Kahn (1999) Eq. 6.16:
      IC_bayes = (σ²_prior × IC_sample + σ²_sample × IC_prior) / (σ²_prior + σ²_sample)

    Intuition: factors with noisy IC estimates (high standard error) are shrunk
    more aggressively toward the prior mean. Stable factors retain their signal.
    """
    if len(ic_map) < 3:
        # Ensure float output even with < 3 factors
        return _extract_float_weights(ic_map)
    import numpy as np
    # Handle both scalars (from cache) and dicts (from compute_ic → {"ic": val, "ir": val})
    raw_vals = list(ic_map.values())
    if raw_vals and isinstance(raw_vals[0], dict):
        values = np.array([v.get("ic_mean", v.get("ic", 0)) if isinstance(v, dict) else v for v in raw_vals])
    else:
        values = np.array(raw_vals)
    ic_prior = float(np.mean(values))
    sigma2_prior = float(np.var(values)) if len(values) > 2 else max(float(np.var(values)), 1e-6)
    if sigma2_prior < 1e-8:
        # All ICs essentially equal — return float weights (not raw dict)
        return _extract_float_weights(ic_map)

    # σ²_sample: approximate standard error of each IC estimate.
    # Using 1/√n assumption where n ≈ 120 trading days (config factor.evaluation.lookback).
    # More precise: each factor's ic_std from factor_ic_daily, but this is a robust default.
    from quant.config.constants import _require_cfg
    n_obs = _require_cfg("factor.evaluation.lookback")
    sigma2_sample = 1.0 / n_obs  # var(IC_est) ≈ 1/n under null

    shrunk = {}
    for name, ic_val in ic_map.items():
        ic = ic_val.get("ic_mean", ic_val.get("ic", ic_val)) if isinstance(ic_val, dict) else ic_val
        numerator = sigma2_prior * ic + sigma2_sample * ic_prior
        denominator = sigma2_prior + sigma2_sample
        shrunk[name] = float(numerator / denominator)

    logger = __import__("quant.utils.logger", fromlist=["get_logger"]).get_logger("quant.factor.stats_cache")
    logger.debug(
        "Bayesian shrinkage: %d factors, prior=%.4f σ²_prior=%.6f → max shrinkage %.0f%%",
        len(shrunk), ic_prior, sigma2_prior,
        (1 - sigma2_prior / (sigma2_prior + sigma2_sample)) * 100
    )
    return shrunk


def _extract_float_weights(ic_map: dict) -> dict:
    """Convert dict-valued ic_map to {name: float_weight} for early-return paths.

    The main path (len >= 3, sigma2_prior >= 1e-8) builds `shrunk` dict inline.
    Early-return paths (too few factors or zero variance) call this to avoid
    returning dict-valued entries to callers that expect floats (e.g. AlphaModel.combine).
    """
    result = {}
    for name, v in ic_map.items():
        result[name] = float(v.get("ic_mean", v.get("ic", v)) if isinstance(v, dict) else v)
    return result

def load_ic_map_from_cache(factor_values: dict = None, scope='live') -> dict:
    """从 DB 加载 IC 权重 (scope 隔离).

    返回: {factor_name: weight} 字典，已归一化。
    factor_values: 可选，用于过滤只保留实际计算出的因子。
    scope: 'live' (实盘, 读 factor_registry) 或 'backtest' (回测, 读 factor_ic_daily).
    """
    return _load_ic_from_db(factor_values, scope=scope)


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
