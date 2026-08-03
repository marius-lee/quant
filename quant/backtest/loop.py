"""Gap 1: Event-driven backtesting loop — walk-forward simulation.

Runs the full pipeline day-by-day over a historical period, simulating
T+1 execution, commissions, lot-size constraints, and stop-losses.

Usage:
    from backtest import run_backtest
    result = run_backtest("2022-01-01", "2024-12-31", capital=5000)
    print(result["metrics"])
"""

from quant.core.phase_tracker import PhaseTracker, PhaseResult
import os, sys, time, uuid as _uuid
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import traceback
from quant.utils.logger import get_logger, set_trace_id, offline_mode
from quant.backtest.analyze import FactorTracker, diagnose, apply_diagnosis
from quant.backtest.broker import SimulatedBroker
from quant.config.constants import _require_cfg
from quant.config import loader as cfgl
from quant.factor.stats_cache import compute_backtest_ic
from quant.alpha.model import AlphaModel

_log = get_logger("backtest.loop")

# Ensure project root on path
_root = os.path.dirname(os.path.dirname(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)
def _get_prices(symbols, date_str, store, field="open"):
    """Get prices from DataStore — reuses connection + LRU cache."""
    syms = list(symbols)
    if not syms:
        return {}
    df = store.get_daily(syms, start=date_str, end=date_str, columns=[field])
    if df.empty or date_str not in df.index:
        return {}
    series = df.loc[date_str, field].dropna()
    return {s: float(v) for s, v in series.items() if v and v > 0}

BACKTEST_DB = os.path.join(_root, "data", "backtest_trades.db")


def _persist_backtest_result(strategy, start, end, capital, metrics, diagnosis, elapsed, avg_signals, errors):
    """ADR-037: 回测结果持久化到 backtest_runs 表，便于历史对比。"""
    import json, sqlite3
    try:
        conn = sqlite3.connect(BACKTEST_DB)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                started_at TEXT DEFAULT (datetime('now','localtime')),
                start_date TEXT, end_date TEXT,
                initial_capital REAL,
                sharpe REAL, cagr_pct REAL, max_dd_pct REAL,
                sortino REAL, calmar REAL, win_rate REAL,
                dsr REAL,
                alpha REAL, info_ratio REAL, beta REAL,
                final_equity REAL, total_return_pct REAL,
                n_days INTEGER, avg_signals REAL,
                errors INTEGER, elapsed_sec REAL,
                diagnosis_json TEXT,
                UNIQUE(strategy, started_at)
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO backtest_runs "
            "(strategy, start_date, end_date, initial_capital, "
            "sharpe, cagr_pct, max_dd_pct, sortino, calmar, win_rate, dsr, "
            "alpha, info_ratio, beta, final_equity, total_return_pct, "
            "n_days, avg_signals, errors, elapsed_sec, diagnosis_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (strategy, start, end, capital,
             metrics.get("sharpe"), metrics.get("cagr_pct"),
             metrics.get("max_drawdown_pct"),
             metrics.get("sortino"), metrics.get("calmar"),
             metrics.get("win_rate"), metrics.get("dsr"),
             metrics.get("alpha"), metrics.get("info_ratio"),
             metrics.get("beta"),
             metrics.get("final_equity"), metrics.get("total_return_pct"),
             metrics.get("n_days"), avg_signals,
             errors, elapsed,
             json.dumps(diagnosis.get("factor_report", {}), default=str)),
        )
        conn.commit()
        conn.close()
        _log.info("backtest: result persisted to backtest_runs")
    except Exception as e:
        _log.warning(f"backtest: failed to persist result (non-fatal): {e}")





def _compute_dsr(returns: pd.Series) -> float | None:
    """ADR-041: Compute Deflated Sharpe Ratio for statistical significance.
    Bailey & Lopez de Prado (2014). DSR < 0.5 → likely overfit.
    """
    try:
        from quant.evaluation.deflated_sharpe import deflated_sharpe_ratio
        vals = returns.dropna().values
        if len(vals) < 20:
            return None
        n_factors = _require_cfg("factor.evaluation.n_symbols")  # proxy for N trials
        sr, dsr = deflated_sharpe_ratio(
            vals, n_trials=max(n_factors, 1),
            skewness=-0.5, kurtosis=8.0,
        )
        return round(dsr, 4)
    except Exception:
        return None


def _compute_backtest_metrics(equity_curve, benchmark_returns=None):
    """Compute Sharpe, MDD, CAGR, win rate, Sortino, Calmar, Alpha, IR, Beta from equity curve."""
    ann_days = _require_cfg("market.annual_trading_days")
    df = pd.DataFrame(equity_curve)
    if df.empty or len(df) < 2:
        return {"sharpe": 0, "max_drawdown_pct": 0, "cagr_pct": 0, "final_equity": 0}

    df["return"] = df["equity"].pct_change()
    returns = df["return"].dropna()

    if len(returns) < 5:
        return {"sharpe": 0, "max_drawdown_pct": 0, "cagr_pct": 0, "final_equity": df["equity"].iloc[-1]}

    # Sharpe (daily → annualized)
    mean_ret = returns.mean()
    std_ret = returns.std()
    ann_days = _require_cfg("market.annual_trading_days")
    sharpe = (mean_ret / std_ret * np.sqrt(ann_days)) if std_ret > 0 else 0.0

    # Max drawdown
    cum = (1 + returns).cumprod()
    running_max = cum.cummax()
    drawdown = (cum - running_max) / running_max
    max_dd = float(drawdown.min())

    # CAGR
    years = len(returns) / ann_days
    final = df["equity"].iloc[-1]
    initial = df["equity"].iloc[0]
    cagr = (final / initial) ** (1 / max(years, 0.5)) - 1 if initial > 0 else 0

    # Win rate
    wins = (returns > 0).sum()
    wr = wins / len(returns) if len(returns) > 0 else 0

    # Sortino (annualized): only penalize downside deviation
    downside = returns[returns < 0]
    if len(downside) > 1 and downside.std() > 0:
        sortino = (mean_ret / downside.std() * np.sqrt(ann_days))
    else:
        sortino = 0.0

    # Calmar: CAGR / |MDD|
    calmar = (cagr / abs(max_dd)) if max_dd < 0 else 0.0

    # Benchmark-relative metrics (Alpha, IR, Beta)
    alpha = None
    ir = None
    beta = None
    if benchmark_returns is not None and not benchmark_returns.empty:
        try:
            bm_returns = benchmark_returns.reindex(returns.index, method='ffill').dropna()
            if bm_returns.empty:
                pass  # 无可用基准数据，跳过 Alpha/IR/Beta
            else:
                common_idx = returns.index.intersection(bm_returns.index)
                if len(common_idx) > 20:
                    strat = returns.loc[common_idx]
                    bm = bm_returns.loc[common_idx]
                    if len(strat) <= 1 or len(bm) <= 1:
                        pass  # 样本不足，无法计算协方差
                    else:
                        cov_mat = np.cov(strat, bm)
                        if cov_mat.shape == (2, 2):
                            bm_var = cov_mat[1, 1]
                            beta_val = 0.0
                            if bm_var > 0:
                                beta_val = cov_mat[0, 1] / bm_var
                                beta = round(float(beta_val), 3)
                            if beta is not None:
                                daily_alpha = (strat - beta_val * bm).mean()
                                alpha = round(float(daily_alpha * ann_days), 4)
                                tracking_err = (strat - bm).std() * np.sqrt(ann_days)
                                if tracking_err > 0:
                                    ir = round(float(daily_alpha * ann_days / tracking_err), 3)
        except (TypeError, ValueError, IndexError) as _e:
            _log.debug("backtest diag compute skipped (non-fatal): %s", _e)

    return {
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd * 100, 1),
        "cagr_pct": round(cagr * 100, 1),
        "final_equity": round(final, 2),
        "initial_equity": round(initial, 2),
        "total_return_pct": round((final / initial - 1) * 100, 1),
        "win_rate": round(wr, 3),
        "n_days": len(returns),
        "sortino": round(sortino, 3),
        "calmar": round(calmar, 3),
        "alpha": alpha,
        "info_ratio": ir,
        "beta": beta,
        # ADR-041: DSR (Deflated Sharpe Ratio)
        "dsr": _compute_dsr(returns),
    }


def run_backtest(start_date=None, end_date=None, capital=5000, strategy=None, retrain_freq=None, mode='full',
                    universe_size=None, ic_lookback=None, factor_status_filter="backtesting",
                    factor_store=None, combine_mode=None):  # deprecated: now auto-initialized from FACTOR_CACHE_DB
    """Run a full walk-forward backtest.

    Args:
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        capital: initial capital in RMB
        strategy: strategy name (uses separate DB to not pollute real trades)
        universe_size: override backtest.universe_size (None=use config)
        ic_lookback: override backtest.diagnosis_ic_window (None=use config)
        factor_status_filter: status filter for get_factor_names (default "backtesting";
            None=all factors)
        factor_store: FactorStore instance (因子值物化缓存). If provided, generate_signals()
            will read from cache instead of re-computing factors each day.
        combine_mode: walk-forward 合成模式覆盖 (None=默认: warmup 后切 ic_weighted)。
            test-v298: hyperopt 把 combine_mode 纳入 Optuna 搜索空间用。

    Returns:
        dict with keys: equity_curve, metrics, signals_per_day, errors
    """
    with offline_mode():
        from quant.execution.calendar import is_trading_day
        from quant.data.store import DataStore
        from quant.execution.engine import ExecutionEngine
        from quant.execution.cost import CostModel
        from quant.backtest.naming import next_backtest_name
        from quant.factor.store import FactorStore
        from quant.config.paths import FACTOR_CACHE_DB

        if strategy is None:
            strategy = next_backtest_name()

        # ── Mode-based defaults: smoke (22d×10 stocks) vs full (244d×all) ──
        if mode == 'smoke':
            if start_date is None or end_date is None:
                end_date = end_date or datetime.now().strftime('%Y-%m-%d')
                start_date = start_date or (pd.Timestamp(end_date) - pd.DateOffset(months=1)).strftime('%Y-%m-%d')
            universe_size = _require_cfg('backtest.smoke.universe_size')  # 10
            _log.info(f'backtest: SMOKE mode — {start_date}→{end_date}, {universe_size} stocks')
        else:  # full
            if end_date is None:
                end_date = datetime.now().strftime('%Y-%m-%d')
            if start_date is None:
                start_date = (pd.Timestamp(end_date) - pd.DateOffset(months=12)).strftime('%Y-%m-%d')
            if universe_size is None:
                u_cfg = cfgl.get('backtest.universe_size'); universe_size = u_cfg if u_cfg is not None else 0
            _log.info(f'backtest: FULL mode — {start_date}→{end_date}, {universe_size or "all"} stocks')

        set_trace_id(_uuid.uuid4().hex[:12])
        _log.info(f"backtest: {start_date} → {end_date}, capital=Y{capital:,}, strategy={strategy}")
        _log.info("=" * 70)
        bt_tracker = PhaseTracker(f"backtest:{strategy}")
        _log.info(f"  BACKTEST START: {strategy} | {start_date} → {end_date} | capital=Y{capital:,}")

        # ── Setup: initialize strategy in backtest DB ──
        engine = ExecutionEngine(db_path=BACKTEST_DB)
        engine.set_initial_capital(strategy, capital)  # always fresh for each run

        # ── Factor cache: use materialized values instead of daily recomputation ──
        _fstore = FactorStore(db_path=FACTOR_CACHE_DB)
        _log.info(f"backtest: factor_store from {FACTOR_CACHE_DB}")

        _log.info(f"backtest: initialized {strategy} with Y{capital:,}")

        # 清理该策略旧交易记录 (防止旧数据污染 get_cash() 计算)
        import sqlite3 as _sql
        _bc = _sql.connect(BACKTEST_DB)
        deleted = _bc.execute("DELETE FROM sim_trades WHERE strategy=?", (strategy,)).rowcount
        if deleted:
            _bc.commit()
            _log.info(f"backtest: cleaned {deleted} old trades for {strategy}")
        _bc.close()

        store = DataStore()
        broker = SimulatedBroker(store, engine, BACKTEST_DB)
        _br = broker  # Python 3.14 兼容: 循环内 try 块通过别名访问
        cost_model = CostModel.from_config()

        # ── Generate trading day list ──
        start_dt = pd.Timestamp(start_date)
        end_dt = pd.Timestamp(end_date)
        all_dates = pd.date_range(start=start_dt, end=end_dt, freq="B")
        trading_days = []
        for d in all_dates:
            ds = d.strftime("%Y-%m-%d")
            if is_trading_day(d.date()):
                trading_days.append(ds)

        if len(trading_days) < _require_cfg("backtest.min_trading_days"):
            _log.error(f"backtest: only {len(trading_days)} trading days — aborting")
            return {"error": f"Too few trading days: {len(trading_days)}"}

        _log.info(f"backtest: {len(trading_days)} trading days to simulate")

        # ── Walk-forward IC ──
        from quant.factor.compute import get_factor_names
        if retrain_freq is None:
            retrain_freq = _require_cfg("alpha.retrain_freq")
        ic_lookback = ic_lookback if ic_lookback is not None else _require_cfg("backtest.diagnosis_ic_window")
        bt_factor_names = get_factor_names(status_filter=factor_status_filter)

        # v385: 验证因子缓存覆盖 IC 回看期, 缺则报清晰错误而非 RuntimeError
        _ic_start = (pd.Timestamp(trading_days[0]) - pd.Timedelta(days=ic_lookback * 2)).strftime("%Y-%m-%d")
        _ic_dates = [d.strftime("%Y-%m-%d") for d in pd.date_range(start=_ic_start, end=trading_days[0], freq="B")
                     if is_trading_day(d.date())]
        if _ic_dates:
            _oldest_needed = _ic_dates[0]
            if not _fstore.is_materialized([_oldest_needed], bt_factor_names[:3]):
                _log.error(
                    f"backtest: factor cache missing for IC lookback period. "
                    f"Oldest needed: {_oldest_needed} (={trading_days[0]} - {ic_lookback}d IC lookback). "
                    f"Rebuild cache: _run('2019-01-01', '{end_date}')"
                )
                return {"error": f"factor_cache_miss: oldest_needed={_oldest_needed}, "
                                 f"run factor_cache._run('2019-01-01', '{end_date}') first"}

        _current_ic_map = compute_backtest_ic(
            start_date=trading_days[0],
            n_train_days=ic_lookback,
            status_filter=factor_status_filter or "backtesting"
        )
        _last_retrain_idx = 0
        _log.info("backtest: initial IC: %d factors, retrain every %dd", len(_current_ic_map), retrain_freq)

        # ── Pre-load all daily data once (eliminates 843 DB queries) ──
        from quant.data.repos import UniverseRepo
        _all_symbols = UniverseRepo().get_symbols(exclude_market='BJ', start_date=start_date, end_date=end_date)
        from quant.factor.windows import max_factor_calendar_days
        _eff_days = max(_require_cfg("data.lookback_days"), max_factor_calendar_days(None))
        _full_start = (pd.Timestamp(trading_days[0]) - pd.Timedelta(days=_eff_days)).strftime("%Y-%m-%d")
        data_full = store.get_daily(_all_symbols, start=_full_start, end=end_date)
        _log.info("backtest: pre-loaded %d days x %d symbols data", len(data_full), len(_all_symbols))

        # v391: ztd 预加载一次 (generate_signals 内每日期 1700 次 → 1 次)
        from quant.factor.compute.price._alternative import preload_ztd_cache
        preload_ztd_cache(trading_days, _all_symbols)
        _log.info("backtest: ztd cache preloaded for %d dates", len(trading_days))

        # ── 预计算共享算子 (原始计算图) ──
        from quant.factor.compute._primitives import precompute_primitives
        _log.info("backtest: precomputing shared primitives...")
        data_prims = precompute_primitives(data_full)
        _log.info("backtest: primitives ready (%d tables)", len(data_prims))

        # ── Diagnostics: factor tracker ──
        tracker = FactorTracker()
        _last_signals = None
        # ── Cooling-off: prevent rebuy after stop-loss ──
        # Q7-2 重构: 冷却注册表收敛到统一 RiskManager (内存 dict — 回测热路径无 DB 写)
        from quant.execution.stop_loss import RiskManager
        _rm = RiskManager(strategy=strategy, cooloff_store={})

        # ── Combine mode: warmup with sleeve, switch to ic_weighted after lookback ──
        warmup_days = _require_cfg("factor.evaluation.lookback")

        # ── rebalance_freq: weekly → 仅调仓日生成信号+再平衡, 非调仓日只跑风控 ──
        _rebalance_freq = _require_cfg("optimizer.rebalance_freq")
        from quant.execution.calendar import is_rebalance_day

        # ── test-v299 §8.2: point-in-time regime (无前视) ──
        # 起始日前训练 HMM; 逐调仓日用截止当日的 benchmark returns 前向滤波。
        # (模块级 get_current_regime 用全量历史训练, 仅实盘可用, 回测禁用)
        _regime_detector = None
        _bm_rets = None
        if _require_cfg("alpha.regime_combine"):
            try:
                from quant.regime.detector import RegimeDetector
                _bm_rets = store.get_benchmark(
                    _require_cfg("backtest.benchmark"),
                    start=_require_cfg("regime.train_start")) * 100
                _train_rets = _bm_rets[_bm_rets.index < pd.Timestamp(start_date)]
                _regime_detector = RegimeDetector().train(_train_rets)
                _log.info("backtest: PIT regime HMM trained on %d days (< %s)",
                          len(_train_rets), start_date)
            except ImportError:
                _log.warning("backtest: hmmlearn not installed, regime detection disabled")
            except Exception as _re:
                _log.warning("backtest: regime detection skipped (non-fatal): %s", _re)

        # ── Main loop ──
        equity_curve = [{"date": trading_days[0], "equity": float(capital)}]
        errors = 0
        signal_counts = []
        t0 = time.time()

        for i, today in enumerate(trading_days[:-1]):
            next_day = trading_days[i + 1]
            _day_t0 = time.time()

            # 调仓日判定 (执行日口径): daily 恒 True; weekly 仅本周首个交易日
            _is_reb = is_rebalance_day(pd.Timestamp(next_day).date(),
                                       freq=_rebalance_freq)

            from quant.pipeline import generate_signals
            # Filter out cooling-off symbols
            cooloff_syms = list(_rm.get_cooloff_symbols(today))
            # B-06 fix: sizing 用当日收盘 MTM 权益 (原成本价 → 无复利且亏损后仍满仓)
            _held = engine.get_positions(strategy)
            _held_close = _get_prices([p["symbol"] for p in _held], today, store, field="close") if _held else {}
            kwargs = {
                "date_str": today,
                "capital": engine.get_capital(strategy, prices=_held_close),
                "strategy": strategy,
                "skip_pull": True,
                "status_filter": factor_status_filter or "backtesting",
                "scope": "backtest",
                "suppress_push": True,
                "universe_size": universe_size,
                "db_path": BACKTEST_DB,
                "store": store,
                "exclude_symbols": cooloff_syms,
                "preloaded_data": data_full,
                "primitives": data_prims,
                "factor_store": _fstore,
            }
            # Switch combine_mode from sleeve (warmup) to ic_weighted (walk-forward);
            # test-v298: run_backtest(combine_mode=...) 可覆盖 walk-forward 模式 (hyperopt)
            if i >= warmup_days:
                kwargs["combine_mode"] = combine_mode or "ic_weighted"  # test-v307: None 时默认切 ic_weighted
            # Walk-forward IC retrain
            if retrain_freq > 0 and (i - _last_retrain_idx) >= retrain_freq and bt_factor_names:
                _log.info("backtest: retraining IC at day %d (%s)", i, today)
                _current_ic_map = compute_backtest_ic(
                    start_date=(pd.Timestamp(today) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    n_train_days=ic_lookback,
                    status_filter=factor_status_filter or "backtesting"
                )
                _last_retrain_idx = i
            kwargs["ic_map"] = _current_ic_map
            # B-22 fix: 单日异常计入 errors 并跳过当日 (原 errors 计数器从未递增,
            # 且单日异常会中断整个回测)
            try:
                if not _is_reb:
                    # 非调仓日 (weekly): 跳过信号生成 (省 ~80% 计算), 只跑硬止损.
                    # 组合不再平衡; 风控每日不断. signal_counts 不计入 (该计数
                    # 描述"信号生成日", 非调仓日本就不生成信号).
                    signals = {"date": today, "target_positions": []}
                    exec_result = _br.execute_risk_only(next_day, strategy=strategy)
                    if exec_result.get("skipped"):
                        equity_curve.append({"date": next_day, "equity": _br.get_mtm_capital(strategy, next_day)})
                        continue
                else:
                    # point-in-time regime 注入 (test-v299 §8.2)
                    if _regime_detector is not None:
                        _rets = _bm_rets[_bm_rets.index <= pd.Timestamp(today)]
                        kwargs["regime_label"], kwargs["regime_probs"] = \
                            _regime_detector.predict_proba(_rets)
                    signals = generate_signals(**kwargs)
                    _last_signals = signals
                    targets = signals.get("target_positions", [])
                    signal_counts.append(len(targets))
                    # ── Record factor contributions for attribution ──
                    fv = signals.get("_factor_values", {})
                    ar = signals.get("_alpha_raw", pd.Series(dtype=float))
                    # Get next-day returns for PnL tracking
                    all_syms_track = list(set([tp["symbol"] for tp in targets]))
                    next_close = _get_prices(all_syms_track, next_day, store, field="close") if all_syms_track and targets else {}
                    today_close = _get_prices(all_syms_track, today, store, field="close") if all_syms_track and targets else {}
                    if isinstance(next_close, dict) and next_close:
                        ret_series = pd.Series({s: (next_close[s] / today_close[s] - 1) for s in next_close if s in today_close and today_close.get(s, 0) > 0})
                    else:
                        ret_series = pd.Series(dtype=float)
                    if fv and not ar.empty and not ret_series.empty:
                        tracker.record_day(today, fv, ar, targets, ret_series)

                    if not targets:
                        # Record equity without trading (B-06: MTM)
                        wealth = _br.get_mtm_capital(strategy, next_day)
                        equity_curve.append({"date": next_day, "equity": wealth})
                        continue

                    # ── Step 2: Execute at next-day open prices ──
                    exec_result = _br.execute(targets, next_day, strategy=strategy)
                    if exec_result.get("skipped"):
                        _log.warning(f"backtest {next_day}: no open prices available, skipping")
                        equity_curve.append({"date": next_day, "equity": engine.get_capital(strategy)})
                        continue
            except Exception as _day_err:
                errors += 1
                _log.error(f"backtest {today}: day failed ({errors} total): {_day_err}")
                equity_curve.append({"date": next_day, "equity": engine.get_capital(strategy)})
                continue

            # ── Step 2.5: Update cooling-off from stop-loss events ──
            stopped = exec_result.get("stopped_out", [])
            if stopped:
                for s in stopped:
                    _rm.set_cooloff(s, next_day)

            bt_tracker.phases.append(PhaseResult(name=f"day_{today}", started=_day_t0, finished=time.time(), status="ok", extra={"signals": len(signals.get("target_positions",[])) if signals else 0}))
            # ── Step 3: Record equity ──
            equity_curve.append({"date": next_day, "equity": exec_result.get("wealth", engine.get_capital(strategy))})

            # Progress log every 60 days
            if (i + 1) % _require_cfg("backtest.progress_log_interval") == 0:
                elapsed = time.time() - t0
                pct_done = (i + 1) / len(trading_days) * 100
                _log.info(f"backtest: {i+1}/{len(trading_days)} days ({pct_done:.0f}%), "
                            f"equity=Y{equity_curve[-1]['equity']:,.2f}, "
                            f"{elapsed:.0f}s elapsed")

        elapsed = time.time() - t0
        # Fetch benchmark returns before closing store
        _bm_levels = store.get_benchmark("000300", start=start_date)
        # B-07 fix: get_benchmark 返回指数点位(如 3900 点), 必须先转日收益率
        # 再与策略日收益算 cov/beta/alpha/IR (此前直接拿点位算, 三个指标全是垃圾值)
        _bm_returns = _bm_levels.pct_change().dropna() if not _bm_levels.empty else _bm_levels
        _bm_returns = _bm_returns.reindex(pd.to_datetime([e["date"] for e in equity_curve]), method='ffill')
        store.close()

        # ── Compute metrics ──
        metrics = _compute_backtest_metrics(equity_curve, _bm_returns)

        # ── Post-backtest diagnosis ──
        _backtest_symbols = []
        if _last_signals:
            fv = _last_signals.get("_factor_values", {})
            sym_set = set()
            for series in fv.values():
                if isinstance(series, pd.Series):
                    sym_set.update(series.dropna().index.tolist())
            _backtest_symbols = list(sym_set)
        ic_map_pre = _current_ic_map  # reuse walk-forward IC (was: compute_pre_backtest_ic)
        diag = diagnose(ic_map_pre, tracker, metrics)

        # ── 回写诊断数据到 evaluation_runs (供 Phase 2 预筛) ──
        # v85 原注释说 run_diagnostics.py 负责, 但该脚本未创建 → 诊断13天未更新
        # v361 恢复: backtest 直接写 evaluation_runs, 同时保留独立脚本兼容性
        passed = [name for name, info in diag.get("factor_report", {}).items()
                  if info.get("recommendation") in ("keep", "boost")]
        try:
            from quant.evaluation.run_store import save_phase
            save_phase("diagnostics", {
                "n_factors": len(diag.get("factor_report", {})),
                "passed": passed,
                "factor_report": {
                    n: {"recommendation": v["recommendation"], "ic_ir": v["ic_ir"]}
                    for n, v in diag.get("factor_report", {}).items()
                },
                "summary": diag.get("summary", ""),
                "backtest_cagr": metrics.get("cagr_pct", 0),
                "backtest_sharpe": metrics.get("sharpe", 0),
            })
            _log.info("diagnostics saved to evaluation_runs: %d passed", len(passed))
        except Exception as _de:
            _log.warning(f"diagnostics save to evaluation_runs failed (non-fatal): {_de}")

        # ── 应用诊断结果: 仅调整 IC 权重 ──
        _adj_ic_map = apply_diagnosis(_current_ic_map, diag)
        # B-22 fix: 调整后的 IC map 此前算出即丢弃 — 随 diagnosis 返回供调用方使用
        diag["adjusted_ic_map"] = _adj_ic_map

        # Stress test on final portfolio holdings
        try:
            from quant.risk.var import stress_test
            _fp = engine.get_positions(strategy)
            if _fp:
                _fw_val = engine.get_capital(strategy)
                _fw = {}
                for _p in _fp:
                    _pv = _p.get("price", 0) * _p.get("shares", 0)
                    _fw[_p["symbol"]] = _pv / max(_fw_val, 1)
                diag["stress_test"] = stress_test(_fp, _fw)
        except Exception as _st_err:
            # Q7-5 fix: stress test 失败必须可观测 (原裸 except: pass 吞错)
            _log.warning(f"stress test skipped (non-fatal): {_st_err}")
        _log.info("diagnosis: %s", diag["summary"])
        for adj in diag["adjustments"]:
            _log.info("  adjust: %s", adj)

        avg_signals = sum(signal_counts) / max(len(signal_counts), 1)

        # test-v344: 回测失败校验 — errors过多或无信号时不持久化,不报告虚假成功
        valid_days = len(trading_days) - errors
        if valid_days == 0 or avg_signals == 0:
            _log.error(f"BACKTEST FAILED: {errors} errors/{len(trading_days)} days, "
                       f"avg_signals={avg_signals:.1f} — result NOT persisted")
            return {"error": "all_days_failed", "errors": errors, "avg_signals": avg_signals, "elapsed": elapsed}

        # ADR-037: 回测结果持久化到 backtest_runs 表
        _persist_backtest_result(strategy, start_date, end_date, capital, metrics, diag, elapsed, avg_signals, errors)
        _log.info("=" * 70)
        _log.info(f"  BACKTEST END: {strategy} | {len(trading_days)}d | elapsed={elapsed:.1f}s "
                    f"| CAGR={metrics['cagr_pct']}% | Sharpe={metrics['sharpe']} | MDD={metrics['max_drawdown_pct']}%")
        _log.info("=" * 70)
        _log.info(f"backtest done in {elapsed:.1f}s: "
                    f"CAGR={metrics['cagr_pct']}%, "
                    f"Sharpe={metrics['sharpe']}, "
                    f"MDD={metrics['max_drawdown_pct']}%, "
                    f"avg_signals/day={avg_signals:.1f}, "
                    f"errors={errors}")

        # Explicit flush — web 服务进程同时在写同一日志文件, Python logging
        # 多进程争用 FileHandler 时可能丢失最后几行. 强制刷盘.
        for h in getattr(_log, "logger", _log).handlers:
            try:
                if hasattr(h, 'flush'):
                    h.flush()
            except Exception as _e:
                _log.warning("backtest: reconcile final skipped (non-fatal): %s", _e)

        return {
            "equity_curve": equity_curve,        "diagnosis": diag,

            "metrics": metrics,
            "avg_signals_per_day": round(avg_signals, 1),
            "errors": errors,
            "elapsed_sec": round(elapsed, 1),
        }


# B-04 fix: BacktestEngine 之前缩进在 run_backtest 函数体内 return 之后,
# 是不可达死代码 (from quant.backtest.loop import BacktestEngine 会 ImportError).
class BacktestEngine:
    """Convenience wrapper for parameterized backtesting."""

    def __init__(self, start="2022-01-01", end="2024-12-31", capital=5000):
        self.start = start
        self.end = end
        self.capital = capital

    def run(self):
        return run_backtest(self.start, self.end, self.capital)

    @property
    def default_params(self):
        return {"start": self.start, "end": self.end, "capital": self.capital}
