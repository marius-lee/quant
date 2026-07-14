"""Gap 1: Event-driven backtesting loop — walk-forward simulation.

Runs the full pipeline day-by-day over a historical period, simulating
T+1 execution, commissions, lot-size constraints, and stop-losses.

Usage:
    from backtest import run_backtest
    result = run_backtest("2022-01-01", "2024-12-31", capital=5000)
    print(result["metrics"])
"""

from core.phase_tracker import PhaseTracker, PhaseResult
import os, sys, time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import traceback
from utils.logger import get_logger
from backtest.diagnostics import FactorTracker, diagnose, apply_diagnosis, compute_pre_backtest_ic
from backtest.broker import SimulatedBroker
from config.constants import _require_cfg
from factor.ic import compute_ic as _compute_ic
from alpha.model import AlphaModel

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





def _compute_backtest_metrics(equity_curve):
    """Compute Sharpe, MDD, CAGR, win rate from equity curve."""
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
    sharpe = (mean_ret / std_ret * np.sqrt(252)) if std_ret > 0 else 0.0

    # Max drawdown
    cum = (1 + returns).cumprod()
    running_max = cum.cummax()
    drawdown = (cum - running_max) / running_max
    max_dd = float(drawdown.min())

    # CAGR
    years = len(returns) / 252
    final = df["equity"].iloc[-1]
    initial = df["equity"].iloc[0]
    cagr = (final / initial) ** (1 / max(years, 0.5)) - 1 if initial > 0 else 0

    # Win rate
    wins = (returns > 0).sum()
    wr = wins / len(returns) if len(returns) > 0 else 0

    return {
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd * 100, 1),
        "cagr_pct": round(cagr * 100, 1),
        "final_equity": round(final, 2),
        "initial_equity": round(initial, 2),
        "total_return_pct": round((final / initial - 1) * 100, 1),
        "win_rate": round(wr, 3),
        "n_days": len(returns),
    }


def run_backtest(start_date, end_date, capital=5000, strategy=None, retrain_freq=None,
                   universe_size=None, ic_lookback=None, factor_status_filter="backtesting"):
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

    Returns:
        dict with keys: equity_curve, metrics, signals_per_day, errors
    """
    from execution.calendar import is_trading_day
    from data.store import DataStore
    from execution.engine import ExecutionEngine
    from execution.cost import CostModel
    from backtest.naming import next_backtest_name

    if strategy is None:
        strategy = next_backtest_name()

    _log.info(f"backtest: {start_date} → {end_date}, capital=Y{capital:,}, strategy={strategy}")
    _log.info("=" * 70)
    bt_tracker = PhaseTracker(f"backtest:{strategy}")
    _log.info(f"  BACKTEST START: {strategy} | {start_date} → {end_date} | capital=Y{capital:,}")

    # ── Setup: initialize strategy in backtest DB ──
    engine = ExecutionEngine(db_path=BACKTEST_DB)
    engine.set_initial_capital(strategy, capital)  # always fresh for each run

    _log.info(f"backtest: initialized {strategy} with Y{capital:,}")

    store = DataStore()
    broker = SimulatedBroker(store, engine, BACKTEST_DB)
    cost_model = CostModel()

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
    from factor.compute import get_factor_names
    if retrain_freq is None:
        retrain_freq = _require_cfg("alpha.retrain_freq")
    ic_lookback = ic_lookback if ic_lookback is not None else _require_cfg("backtest.diagnosis_ic_window")
    bt_factor_names = get_factor_names(status_filter=factor_status_filter)
    _current_ic_map_raw = _compute_ic(
        factor_names=bt_factor_names, date=trading_days[0],
        symbols=store.get_universe(trading_days[0])[:_require_cfg("factor.evaluation.n_symbols")],
        lookback=ic_lookback, store=store, status_filter=factor_status_filter or "backtesting"
    )
    _last_retrain_idx = 0
    _current_ic_map = _current_ic_map_raw["ic_map"]
    _log.info("backtest: initial IC: %d factors, retrain every %dd", len(_current_ic_map), retrain_freq)

    
    # ── Diagnostics: factor tracker ──
    tracker = FactorTracker()
    _last_signals = None
    # ── Cooling-off: prevent rebuy after stop-loss ──
    _cooloff = {}  # {symbol: end_date}

    # ── Combine mode: warmup with sleeve, switch to ic_weighted after lookback ──
    warmup_days = _require_cfg("factor.evaluation.lookback")

    # ── Main loop ──
    equity_curve = [{"date": trading_days[0], "equity": float(capital)}]
    errors = 0
    signal_counts = []
    t0 = time.time()

    for i, today in enumerate(trading_days[:-1]):
        next_day = trading_days[i + 1]
        _day_t0 = time.time()

        from pipeline import generate_signals
        # Filter out cooling-off symbols
        cooloff_syms = [s for s, d in _cooloff.items() if pd.Timestamp(d) > pd.Timestamp(today)]
        kwargs = {
            "date_str": today,
            "capital": engine.get_capital(strategy),
            "strategy": strategy,
            "skip_pull": True,
            "status_filter": factor_status_filter or "backtesting",
            "suppress_push": True,
            "universe_size": universe_size if universe_size is not None else _require_cfg("backtest.universe_size"),
            "db_path": BACKTEST_DB,
            "store": store,
            "exclude_symbols": cooloff_syms,
        }
        # Switch combine_mode from sleeve (warmup) to ic_weighted (walk-forward)
        if i >= warmup_days:
            kwargs["combine_mode"] = "ic_weighted"
        # Walk-forward IC retrain
        if retrain_freq > 0 and (i - _last_retrain_idx) >= retrain_freq and bt_factor_names:
            _log.info("backtest: retraining IC at day %d (%s)", i, today)
            _current_ic_map_raw = _compute_ic(
                factor_names=bt_factor_names, date=pd.Timestamp(today) - pd.Timedelta(days=1),
                symbols=store.get_universe(today)[:_require_cfg("factor.evaluation.n_symbols")],
                lookback=ic_lookback, store=store, status_filter=factor_status_filter or "backtesting"
            )
            _last_retrain_idx = i
        kwargs["ic_map"] = _current_ic_map
        signals = generate_signals(**kwargs)
        _last_signals = signals
        targets = signals.get("target_positions", [])
        signal_counts.append(len(targets))
        # ── Record factor contributions for attribution ──
        fv = signals.get("_factor_values", {})
        ar = signals.get("_alpha_raw", pd.Series(dtype=float))
        # Get next-day returns for PnL tracking
        all_syms_track = list(set([tp["symbol"] for tp in targets]))
        next_ret = _get_prices(all_syms_track, next_day, store, field="close") if all_syms_track and targets else {}
        if isinstance(next_ret, dict) and next_ret:
            ret_series = pd.Series(next_ret)
        else:
            ret_series = pd.Series(dtype=float)
        if fv and not ar.empty and not ret_series.empty:
            tracker.record_day(today, fv, ar, targets, ret_series)

        if not targets:
            # Record equity without trading
            wealth = engine.get_capital(strategy)
            equity_curve.append({"date": next_day, "equity": wealth})
            continue

        # ── Step 2: Execute at next-day open prices ──
        exec_result = broker.execute(targets, next_day, strategy=strategy)
        if exec_result.get("skipped"):
            _log.warning(f"backtest {next_day}: no open prices available, skipping")
            equity_curve.append({"date": next_day, "equity": broker.get_capital(strategy)})
            continue

        # ── Step 2.5: Update cooling-off from stop-loss events ──
        stopped = exec_result.get("stopped_out", [])
        if stopped:
            cooloff_end = pd.Timestamp(next_day) + pd.Timedelta(days=_require_cfg("risk.stop_loss_cooloff_days"))
            for s in stopped:
                _cooloff[s] = cooloff_end.strftime("%Y-%m-%d")

        bt_tracker.phases.append(PhaseResult(name=f"day_{today}", started=_day_t0, finished=time.time(), status="ok", extra={"signals": len(signals.get("target_positions",[])) if signals else 0}))
        # ── Step 3: Record equity ──
        equity_curve.append({"date": next_day, "equity": exec_result.get("wealth", broker.get_capital(strategy))})

        # Progress log every 60 days
        if (i + 1) % _require_cfg("backtest.progress_log_interval") == 0:
            elapsed = time.time() - t0
            pct_done = (i + 1) / len(trading_days) * 100
            _log.info(f"backtest: {i+1}/{len(trading_days)} days ({pct_done:.0f}%), "
                      f"equity=Y{equity_curve[-1]['equity']:,.2f}, "
                      f"{elapsed:.0f}s elapsed")

    elapsed = time.time() - t0
    store.close()

    # ── Compute metrics ──
    metrics = _compute_backtest_metrics(equity_curve)

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

    # ── 自动应用诊断结果: 调整权重 + 自动退休未使用因子 ──
    _adj_ic_map = apply_diagnosis(_current_ic_map, diag)
    _dropped = [name for name, info in diag.get("factor_report", {}).items()
                if info.get("recommendation") == "drop" and info.get("n_trades", 0) == 0]
    if _dropped:
        _log.info("auto-retiring %d unused factors after backtest", len(_dropped))
        from data.repos import FactorRepo
        _frepo = FactorRepo()
        _frepo.batch_set_status(_dropped, "retired", "auto: unused in backtest")

    # Stress test on final portfolio holdings
    try:
        from risk.var import stress_test
        _fp = engine.get_positions(strategy)
        if _fp:
            _fw_val = engine.get_capital(strategy)
            _fw = {}
            for _p in _fp:
                _pv = _p.get("price", 0) * _p.get("shares", 0)
                _fw[_p["symbol"]] = _pv / max(_fw_val, 1)
            diag["stress_test"] = stress_test(_fp, _fw)
    except Exception:
        pass
    _log.info("diagnosis: %s", diag["summary"])
    for adj in diag["adjustments"]:
        _log.info("  adjust: %s", adj)

    # ── 两步架构 Step 1: 诊断结果持久化 ──
    # 写入 evaluation_runs (供 Step 2 正式评估做预筛)
    from evaluation.run_store import save_phase
    passed = [name for name, info in diag.get("factor_report", {}).items()
              if info.get("recommendation") in ("keep", "boost")]
    save_phase("diagnostics", {
        "n_factors": len(diag.get("factor_report", {})),
        "passed": passed,
        "factor_report": diag.get("factor_report", {}),
        "adjustments": diag.get("adjustments", []),
        "backtest_strategy": strategy,
        "backtest_period": f"{start_date}_{end_date}",
        "sharpe": metrics.get("sharpe", 0),
        "cagr_pct": metrics.get("cagr_pct", 0),
    })
    _log.info("diagnosis saved to evaluation_runs: %d passed", len(passed))

    # 更新 factor_registry.status_reason (只改 backtesting 因子)
    import sqlite3 as _sqlite
    _conn = _sqlite.connect(os.path.join(_root, "data", "market.db"))
    _today = datetime.now().strftime("%Y-%m-%d")
    for name, info in diag.get("factor_report", {}).items():
        rec = info.get("recommendation", "keep")
        ir_val = info.get("ic_ir", 0)
        pnl_val = info.get("pnl_contrib", 0)
        reason = f"diag:{rec}(ICIR={ir_val:.2f},PnL={pnl_val:.3f},{_today})"
        _conn.execute(
            "UPDATE factor_registry SET status_reason=?, updated_at=datetime('now','localtime') "
            "WHERE name=? AND status IN ('registered','candidate','retired')",
            (reason, name)
        )
    _conn.commit()
    _conn.close()


    avg_signals = sum(signal_counts) / max(len(signal_counts), 1)
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

    return {
        "equity_curve": equity_curve,        "diagnosis": diag,

        "metrics": metrics,
        "avg_signals_per_day": round(avg_signals, 1),
        "errors": errors,
        "elapsed_sec": round(elapsed, 1),
    }


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
