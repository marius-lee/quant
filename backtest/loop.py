"""Gap 1: Event-driven backtesting loop — walk-forward simulation.

Runs the full pipeline day-by-day over a historical period, simulating
T+1 execution, commissions, lot-size constraints, and stop-losses.

Usage:
    from backtest import run_backtest
    result = run_backtest("2022-01-01", "2024-12-31", capital=5000)
    print(result["metrics"])
"""

import os, sys, time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import traceback
from utils.logger import get_logger
from backtest.diagnostics import FactorTracker, diagnose, compute_pre_backtest_ic
from config.loader import get as _cfg
from factor.ic import compute_ic as _compute_ic
from alpha.model import AlphaModel

_log = get_logger("backtest.loop")

# Ensure project root on path
_root = os.path.dirname(os.path.dirname(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

BACKTEST_DB = os.path.join(_root, "data", "backtest_trades.db")


def _get_open_prices(symbols, date_str, store):
    """Get opening prices — chunked to avoid SQLite variable limit (max 999)."""
    import sqlite3
    result = {}
    syms = list(symbols)
    chunk_size = 500
    try:
        conn = sqlite3.connect(os.path.join(_root, "data", "market.db"))
        for i in range(0, len(syms), chunk_size):
            chunk = syms[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT symbol, open FROM daily WHERE date=? AND symbol IN ({placeholders})",
                [date_str] + chunk
            ).fetchall()
            for r in rows:
                if r[1] and r[1] > 0:
                    result[r[0]] = r[1]
        conn.close()
    except Exception as e:
        _log.error(f"_get_open_prices({date_str}) traceback:\n{traceback.format_exc()}")
        _log.warning(f"_get_open_prices({date_str}): {e}")
    return result


def _get_close_prices(symbols, date_str, store):
    """Get closing prices — chunked to avoid SQLite variable limit (max 999)."""
    import sqlite3
    result = {}
    syms = list(symbols)
    chunk_size = 500
    try:
        conn = sqlite3.connect(os.path.join(_root, "data", "market.db"))
        for i in range(0, len(syms), chunk_size):
            chunk = syms[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT symbol, close FROM daily WHERE date=? AND symbol IN ({placeholders})",
                [date_str] + chunk
            ).fetchall()
            for r in rows:
                if r[1] and r[1] > 0:
                    result[r[0]] = r[1]
        conn.close()
    except Exception as e:
        _log.error(f"_get_close_prices({date_str}) traceback:\n{traceback.format_exc()}")
        _log.warning(f"_get_close_prices({date_str}): {e}")
    return result


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


def run_backtest(start_date, end_date, capital=5000, strategy=None, retrain_freq=None):
    """Run a full walk-forward backtest.

    Args:
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        capital: initial capital in RMB
        strategy: strategy name (uses separate DB to not pollute real trades)

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
    _log.info(f"  BACKTEST START: {strategy} | {start_date} → {end_date} | capital=Y{capital:,}")

    # ── Setup: initialize strategy in backtest DB ──
    engine = ExecutionEngine(db_path=BACKTEST_DB)
    engine.set_initial_capital(strategy, capital)  # always fresh for each run

    _log.info(f"backtest: initialized {strategy} with Y{capital:,}")

    store = DataStore()
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

    if len(trading_days) < _cfg("backtest.min_trading_days"):
        _log.error(f"backtest: only {len(trading_days)} trading days — aborting")
        return {"error": f"Too few trading days: {len(trading_days)}"}

    _log.info(f"backtest: {len(trading_days)} trading days to simulate")

    # ── Walk-forward IC ──
    from factor.compute import get_factor_names
    if retrain_freq is None:
        retrain_freq = _cfg("alpha.retrain_freq")
    ic_lookback = _cfg("backtest.diagnosis_ic_window")
    bt_factor_names = get_factor_names(status_filter="backtesting")
    _current_ic_map_raw = _compute_ic(
        factor_names=bt_factor_names, date=trading_days[0],
        symbols=store.get_universe(trading_days[0])[:_cfg("factor.evaluation.n_symbols")],
        lookback=ic_lookback, store=store, status_filter="backtesting"
    )
    _last_retrain_idx = 0
    _current_ic_map = _current_ic_map_raw["ic_map"]
    _log.info("backtest: initial IC: %d factors, retrain every %dd", len(_current_ic_map), retrain_freq)

    
    # ── Diagnostics: factor tracker ──
    tracker = FactorTracker()
    _last_signals = None
    # ── Cooling-off: prevent rebuy after stop-loss ──
    _cooloff = {}  # {symbol: end_date}

    # ── Main loop ──
    equity_curve = [{"date": trading_days[0], "equity": float(capital)}]
    errors = 0
    signal_counts = []
    t0 = time.time()

    for i, today in enumerate(trading_days[:-1]):
        next_day = trading_days[i + 1]

        try:
            # ── Step 1: Generate signals using data up to 'today' ──
            from pipeline import generate_signals
            # Filter out cooling-off symbols
            cooloff_syms = [s for s, d in _cooloff.items() if pd.Timestamp(d) > pd.Timestamp(today)]
            kwargs = {
                "date_str": today,
                "capital": engine.get_capital(strategy),
                "strategy": strategy,
                "skip_pull": True,
                "status_filter": "backtesting",
                "suppress_push": True,
                "universe_size": _cfg("backtest.universe_size"),
                "db_path": BACKTEST_DB,
                "store": store,
                "exclude_symbols": cooloff_syms,
            }
            # Walk-forward IC retrain
            if retrain_freq > 0 and (i - _last_retrain_idx) >= retrain_freq and bt_factor_names:
                _log.info("backtest: retraining IC at day %d (%s)", i, today)
                _current_ic_map_raw = _compute_ic(
                    factor_names=bt_factor_names, date=today,
                    symbols=store.get_universe(today)[:_cfg("factor.evaluation.n_symbols")],
                    lookback=ic_lookback, store=store, status_filter="backtesting"
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
            next_ret = _get_close_prices(all_syms_track, next_day, store) if all_syms_track and targets else {}
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
            all_syms = set()
            for tp in targets:
                all_syms.add(tp["symbol"])
            current = engine.get_positions(strategy)
            for p in current:
                all_syms.add(p["symbol"])

            open_prices = _get_open_prices(list(all_syms), next_day, store)

            if not open_prices:
                _log.warning(f"backtest {next_day}: no open prices available, skipping")
                equity_curve.append({"date": next_day, "equity": engine.get_capital(strategy)})
                continue

            # Execute with open prices override
            from pipeline import execute_signals
            exec_result = execute_signals(
                targets, next_day, strategy=strategy,
                prices=open_prices,
                db_path=BACKTEST_DB,
                suppress_push=True,
            )

            # ── Step 2.5: Update cooling-off from stop-loss events ──
            stopped = exec_result.get("stopped_out", [])
            if stopped:
                cooloff_end = pd.Timestamp(next_day) + pd.Timedelta(days=_cfg("risk.stop_loss_cooloff_days"))
                for s in stopped:
                    _cooloff[s] = cooloff_end.strftime("%Y-%m-%d")

            # ── Step 3: Record equity ──
            wealth = engine.get_capital(strategy)
            equity_curve.append({"date": next_day, "equity": wealth})

        except Exception as e:
            errors += 1
            _log.error(f"backtest {today}: traceback:\n{traceback.format_exc()}")
            _log.warning(f"backtest {today}: error ({errors}): {e}")
            last_equity = equity_curve[-1]["equity"] if equity_curve else capital
            equity_curve.append({"date": next_day, "equity": last_equity})

        # Progress log every 60 days
        if (i + 1) % _cfg("backtest.progress_log_interval") == 0:
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
    try:
        # Compute pre-backtest IC for factor evaluation
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
        _log.info("diagnosis: %s", diag["summary"])
        for adj in diag["adjustments"]:
            _log.info("  adjust: %s", adj)

        # ── 两步架构 Step 1: 诊断结果持久化 ──
        # 写入 evaluation_runs (供 Step 2 正式评估做预筛)
        try:
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
        except Exception as _se:
            _log.error("diagnosis save_phase traceback:\n%s", traceback.format_exc())

        # 更新 factor_registry.status_reason (只改 backtesting 因子)
        try:
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
        except Exception as _fe:
            _log.error("diagnosis factor_registry update traceback:\n%s", traceback.format_exc())

    except Exception as e:
        _log.error("diagnosis traceback:\n%s", traceback.format_exc())
        _log.warning("diagnosis failed: %s", e)
        diag = {"factor_report": {}, "adjustments": [], "summary": str(e)}


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
