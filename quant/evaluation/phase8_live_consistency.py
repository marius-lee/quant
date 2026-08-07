"""Phase 8: Backtest-vs-Live Consistency Cross-Validation.

Compares backtest simulation against actual live trading results across four
dimensions: signals, execution fills, costs, and equity curves.  Runs as a
periodic diagnostic — detects drift between simulation assumptions and real
market conditions before they compound into material PnL errors.

Design (Pesaran & Timmermann 2000, Bailey et al. 2014):
  D1. Signal consistency — same date, same pipeline → same targets?
  D2. Execution consistency — same order → same fill price?
  D3. Cost consistency — CostModel estimate vs actual commission/slippage?
  D4. Equity consistency — backtest equity vs live equity on overlapping days?

Data sources:
  Live  — TradeRepo (sim_trades / daily_signals / daily_equity / pending_orders)
  Backtest — run_backtest() over the live trading period, same factor pool,
             same capital, same config.

Graceful degradation: if live data is insufficient (fewer than N trading days
or zero filled trades), returns "insufficient_data" status with whatever
partial comparisons are available rather than failing.

Usage:
    PYTHONPATH=. python3 -c "
      from quant.evaluation.phase8_live_consistency import validate_consistency
      result = validate_consistency()
      print(result['status'], result['dimensions'])
    "
"""

import os, sys, json, time
_root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _root)

import pandas as pd
import numpy as np
from datetime import datetime
from scipy import stats as _stats

from quant.utils.logger import get_logger

_log = get_logger("evaluation.phase8")

# ── Minimum data thresholds ──
MIN_LIVE_DAYS = 5       # fewer than 5 live days → "insufficient_data"
MIN_LIVE_TRADES = 1     # fewer than 1 filled trade → skip execution/cost dims

# ── Thresholds for flagging divergence (config-driven where possible) ──
SIGNAL_MATCH_WARN = 0.70     # < 70% symbol overlap → warning
FILL_PRICE_MAE_WARN = 0.03   # > 3% mean absolute fill price error → warning
COST_MAE_WARN = 10.0         # > ¥10 mean cost error per trade → warning
EQUITY_DIVERGE_WARN = 0.05   # > 5% final equity divergence → warning


def _get_live_date_range() -> tuple[str | None, str | None]:
    """Find the earliest and latest live signal dates from daily_signals.

    Returns (start_date, end_date) or (None, None) if no live signals exist.
    """
    from quant.data.repos.trade_repo import TradeRepo
    tr = TradeRepo()
    try:
        row = tr._query_one(
            "SELECT MIN(date), MAX(date) FROM daily_signals WHERE mode='live'"
        )
        if row and row[0]:
            return row[0], row[1]
        return None, None
    finally:
        pass


def _compare_signals(live_start: str, live_end: str) -> dict:
    """D1: Compare backtest-generated signals against live signals.

    Runs generate_signals() for each live trading day in backtest mode,
    then compares target positions symbol-by-symbol against stored live signals.

    Returns dict with match rate, score correlation, and per-day detail.
    """
    from quant.data.repos.trade_repo import TradeRepo
    from quant.pipeline import generate_signals
    from quant.execution.calendar import is_trading_day as _is_td

    tr = TradeRepo()

    # ── Load live signals ──
    live_signals_raw = tr.get_daily_signals_range(start=live_start, end=live_end, mode="live")
    if not live_signals_raw:
        return {"status": "no_live_signals", "match_rate": None, "details": []}

    live_by_date: dict[str, list[dict]] = {}
    for row in live_signals_raw:
        d = row["date"]
        sigs = json.loads(row["signals_json"]) if isinstance(row.get("signals_json"), str) else row.get("signals_json", [])
        live_by_date[d] = sigs

    # ── Generate backtest signals for each live date ──
    # v406: 修复 NameError (factor_store 未定义)
    from quant.factor.store import FactorStore
    factor_store = FactorStore()
    dates_sorted = sorted(live_by_date.keys())
    all_dates = [d for d in pd.date_range(dates_sorted[0], dates_sorted[-1], freq="B")
                 if _is_td(d.date())]
    bt_signals: dict[str, list[dict]] = {}
    for d_str in all_dates:
        d_str = d_str.strftime("%Y-%m-%d") if hasattr(d_str, "strftime") else str(d_str)
        if d_str not in live_by_date:
            continue
        try:
            result = generate_signals(
                date_str=d_str, skip_pull=True, suppress_push=True,
                scope="backtest", status_filter="backtesting",
                factor_store=factor_store,
            )
            bt_signals[d_str] = result.get("target_positions", [])
        except Exception as e:
            _log.warning(f"D1: failed to generate backtest signals for {d_str}: {e}")
            bt_signals[d_str] = []

    # ── Compare day-by-day ──
    details = []
    all_live_syms = set()
    all_bt_syms = set()
    matched_pairs = []  # (live_score, bt_score)
    share_pairs = []    # (live_shares, bt_shares)

    for d_str in sorted(live_by_date.keys()):
        live_sigs = live_by_date.get(d_str, [])
        bt_sigs = bt_signals.get(d_str, [])

        live_map = {s["symbol"]: s for s in live_sigs}
        bt_map = {s["symbol"]: s for s in bt_sigs}

        live_syms = set(live_map.keys())
        bt_syms = set(bt_map.keys())
        all_live_syms |= live_syms
        all_bt_syms |= bt_syms

        common = live_syms & bt_syms
        only_live = live_syms - bt_syms
        only_bt = bt_syms - live_syms

        match_rate = len(common) / max(len(live_syms | bt_syms), 1)
        for sym in common:
            matched_pairs.append((live_map[sym].get("score", 0), bt_map[sym].get("score", 0)))
            share_pairs.append((live_map[sym].get("shares", 0), bt_map[sym].get("shares", 0)))

        details.append({
            "date": d_str,
            "match_rate": round(match_rate, 4),
            "common": list(common),
            "only_live": list(only_live),
            "only_backtest": list(only_bt),
        })

    # ── Aggregate metrics ──
    overall_match = len(all_live_syms & all_bt_syms) / max(len(all_live_syms | all_bt_syms), 1)

    score_corr = None
    if len(matched_pairs) >= 3:
        live_scores = [p[0] for p in matched_pairs]
        bt_scores = [p[1] for p in matched_pairs]
        rho, pval = _stats.spearmanr(live_scores, bt_scores)
        score_corr = round(float(rho), 4) if not np.isnan(rho) else None

    share_corr = None
    if len(share_pairs) >= 3:
        live_shares = [p[0] for p in share_pairs]
        bt_shares = [p[1] for p in share_pairs]
        rho, pval = _stats.spearmanr(live_shares, bt_shares)
        share_corr = round(float(rho), 4) if not np.isnan(rho) else None

    status = "ok"
    if overall_match < SIGNAL_MATCH_WARN:
        status = "divergent"

    return {
        "status": status,
        "n_live_dates": len(live_by_date),
        "n_backtest_dates": len(bt_signals),
        "overall_match_rate": round(overall_match, 4),
        "score_spearman_r": score_corr,
        "share_spearman_r": share_corr,
        "details": details,
    }


def _compare_execution(live_start: str, live_end: str) -> dict:
    """D2: Compare backtest fill prices against live fill prices.

    Runs a mini backtest over the live period, then matches trades by
    (date, symbol, side) and computes fill price errors.

    Returns dict with MAE, MAPE, and per-trade detail.
    """
    from quant.data.repos.trade_repo import TradeRepo
    tr = TradeRepo()

    live_trades = tr.get_trades(mode="live", limit=10000)
    live_trades = [t for t in live_trades if live_start <= t["date"] <= live_end]
    if len(live_trades) < MIN_LIVE_TRADES:
        return {"status": "insufficient_data", "n_live_trades": len(live_trades), "mae_pct": None, "details": []}

    # ── Run backtest over same period ──
    from quant.backtest.loop import run_backtest
    bt_result = run_backtest(
        start_date=live_start, end_date=live_end,
        capital=_get_live_initial_capital(),
        strategy="phase8_bt",
        suppress_push=True,
    )

    bt_trades = bt_result.get("trades", [])
    bt_by_key = {}
    for t in bt_trades:
        key = (t["date"], t["symbol"], t["side"])
        bt_by_key[key] = t

    # ── Match and compare ──
    price_errors_pct = []
    details = []
    for lt in live_trades:
        key = (lt["date"], lt["symbol"], lt.get("side", "buy"))
        bt = bt_by_key.get(key)
        if bt and bt.get("price", 0) > 0:
            err = abs(lt["price"] - bt["price"]) / lt["price"]
            price_errors_pct.append(err)
            details.append({
                "date": lt["date"], "symbol": lt["symbol"], "side": lt.get("side", "buy"),
                "live_price": lt["price"], "backtest_price": bt["price"],
                "error_pct": round(err, 4),
            })

    if not price_errors_pct:
        return {"status": "no_matches", "n_live_trades": len(live_trades), "mae_pct": None, "details": []}

    mae = np.mean(price_errors_pct)
    mape = mae  # already percentage
    status = "ok" if mae < FILL_PRICE_MAE_WARN else "divergent"

    return {
        "status": status,
        "n_live_trades": len(live_trades),
        "n_matched": len(price_errors_pct),
        "mae_pct": round(float(mae), 4),
        "details": details,
    }


def _compare_costs(live_start: str, live_end: str) -> dict:
    """D3: Compare CostModel estimates against actual live commissions/slippage.

    For each filled live trade, compute what CostModel would have estimated
    and compare with actual sim_trades.cost.
    """
    from quant.data.repos.trade_repo import TradeRepo
    from quant.execution.cost import CostModel

    tr = TradeRepo()
    cm = CostModel()

    live_trades = tr.get_trades(mode="live", limit=10000)
    live_trades = [t for t in live_trades if live_start <= t["date"] <= live_end]
    fills = [t for t in live_trades if t.get("filled_price") or t.get("price", 0) > 0]
    if len(fills) < MIN_LIVE_TRADES:
        return {"status": "insufficient_data", "n_fills": len(fills), "mae_yuan": None, "details": []}

    cost_errors = []
    details = []
    for t in fills:
        px = t.get("filled_price") or t.get("price", 0)
        shares = t.get("shares", 0) or t.get("filled_shares", 0)
        side = t.get("side", "buy")
        actual_cost = t.get("cost", 0)
        estimated_cost = cm.buy_cost(px, shares) if side == "buy" else cm.sell_cost(px, shares)
        err = abs(estimated_cost - actual_cost)
        cost_errors.append(err)
        details.append({
            "date": t["date"], "symbol": t["symbol"], "side": side,
            "estimated": round(estimated_cost, 2), "actual": round(actual_cost, 2),
            "error_yuan": round(err, 2),
        })

    mae = np.mean(cost_errors)
    status = "ok" if mae < COST_MAE_WARN else "divergent"

    return {
        "status": status,
        "n_fills": len(fills),
        "mae_yuan": round(float(mae), 2),
        "details": details,
    }


def _compare_equity(live_start: str, live_end: str) -> dict:
    """D4: Compare backtest equity curve against live daily_equity.

    Computes final equity divergence and daily return correlation.
    """
    from quant.data.repos.trade_repo import TradeRepo
    from quant.backtest.loop import run_backtest

    tr = TradeRepo()

    # ── Live equity ──
    live_rows = tr.get_daily_equity_range(start=live_start, end=live_end)
    if not live_rows or len(live_rows) < 2:
        return {"status": "insufficient_data", "n_live_days": len(live_rows), "final_divergence_pct": None}

    live_dates = [r["date"] for r in live_rows]
    live_equity = [r["equity"] for r in live_rows]

    # ── Backtest equity ──
    bt_result = run_backtest(
        start_date=live_dates[0], end_date=live_dates[-1],
        capital=live_equity[0],
        strategy="phase8_bt_eq",
        suppress_push=True,
    )
    bt_curve = bt_result.get("equity_curve", [])
    bt_by_date = {e["date"]: e["equity"] for e in bt_curve if "date" in e}

    # ── Align and compare ──
    common_dates = sorted(set(live_dates) & set(bt_by_date.keys()))
    if len(common_dates) < 2:
        return {"status": "no_overlap", "n_common_dates": len(common_dates), "final_divergence_pct": None}

    live_aligned = [next(r["equity"] for r in live_rows if r["date"] == d) for d in common_dates]
    bt_aligned = [bt_by_date[d] for d in common_dates]

    final_div = abs(live_aligned[-1] - bt_aligned[-1]) / max(live_aligned[-1], 1)
    live_ret = pd.Series(live_aligned).pct_change().dropna()
    bt_ret = pd.Series(bt_aligned).pct_change().dropna()

    ret_corr = None
    if len(live_ret) >= 3 and len(bt_ret) >= 3:
        common_len = min(len(live_ret), len(bt_ret))
        rho, _ = _stats.spearmanr(live_ret.iloc[-common_len:], bt_ret.iloc[-common_len:])
        ret_corr = round(float(rho), 4) if not np.isnan(rho) else None

    status = "ok" if final_div < EQUITY_DIVERGE_WARN else "divergent"

    return {
        "status": status,
        "n_common_dates": len(common_dates),
        "final_divergence_pct": round(float(final_div) * 100, 2),
        "return_correlation": ret_corr,
        "live_final": round(live_aligned[-1], 2),
        "backtest_final": round(bt_aligned[-1], 2),
    }


def _get_live_initial_capital() -> float:
    """Read initial capital from strategy_config."""
    from quant.data.repos.trade_repo import TradeRepo
    tr = TradeRepo()
    cfg = tr.get_strategy_config("quant")
    return float(cfg.get("initial_capital", 5000))


# ── Public API ──

def validate_consistency() -> dict:
    """Run all four dimensions of backtest-vs-live consistency validation.

    Returns:
        {
            "status": "ok" | "insufficient_data" | "divergent",
            "dimensions": { D1..D4 results },
            "overall_score": 0-100 or None,
            "live_date_range": [start, end],
        }
    """
    from quant.evaluation.run_store import save_phase

    set_trace_id = None
    try:
        from quant.utils.logger import set_trace_id as _sti
        set_trace_id = _sti
    except Exception as _e:
        _log.debug("set_trace_id import failed (non-fatal): %s", _e)

    _log.info("Phase 8: backtest-vs-live consistency validation starting...")
    t0 = time.time()

    live_start, live_end = _get_live_date_range()
    if not live_start:
        _log.warning("Phase 8: no live signals found — nothing to compare")
        result = {
            "status": "insufficient_data",
            "live_date_range": None,
            "dimensions": {},
            "overall_score": None,
            "elapsed_sec": round(time.time() - t0, 1),
        }
        save_phase("phase8", result)
        return result

    _log.info(f"Phase 8: live period detected = {live_start} → {live_end}")

    n_live_days = max(1, (pd.Timestamp(live_end) - pd.Timestamp(live_start)).days // 7 * 5)

    dims = {}

    # D1: Signal consistency (always run if we have live dates)
    try:
        dims["signals"] = _compare_signals(live_start, live_end)
        _log.info(f"Phase 8 D1 signals: status={dims['signals']['status']}")
    except Exception as e:
        _log.error(f"Phase 8 D1 signals failed: {e}")
        dims["signals"] = {"status": "error", "error": str(e)}

    # D2-D4: only if we have enough live data
    has_enough = n_live_days >= MIN_LIVE_DAYS
    if not has_enough:
        _log.info(f"Phase 8: only {n_live_days} live days (min {MIN_LIVE_DAYS}) — D2-D4 skipped")
        dims["execution"] = {"status": "insufficient_data", "reason": f"need >= {MIN_LIVE_DAYS} live days, have ~{n_live_days}"}
        dims["costs"] = {"status": "insufficient_data", "reason": f"need >= {MIN_LIVE_DAYS} live days, have ~{n_live_days}"}
        dims["equity"] = {"status": "insufficient_data", "reason": f"need >= {MIN_LIVE_DAYS} live days, have ~{n_live_days}"}
    else:
        # D2: Execution
        try:
            dims["execution"] = _compare_execution(live_start, live_end)
            _log.info(f"Phase 8 D2 execution: status={dims['execution']['status']}")
        except Exception as e:
            _log.error(f"Phase 8 D2 execution failed: {e}")
            dims["execution"] = {"status": "error", "error": str(e)}

        # D3: Costs
        try:
            dims["costs"] = _compare_costs(live_start, live_end)
            _log.info(f"Phase 8 D3 costs: status={dims['costs']['status']}")
        except Exception as e:
            _log.error(f"Phase 8 D3 costs failed: {e}")
            dims["costs"] = {"status": "error", "error": str(e)}

        # D4: Equity
        try:
            dims["equity"] = _compare_equity(live_start, live_end)
            _log.info(f"Phase 8 D4 equity: status={dims['equity']['status']}")
        except Exception as e:
            _log.error(f"Phase 8 D4 equity failed: {e}")
            dims["equity"] = {"status": "error", "error": str(e)}

    # ── Overall score ──
    scores = []
    weights = {"signals": 0.3, "execution": 0.3, "costs": 0.2, "equity": 0.2}
    for dim, w in weights.items():
        d = dims.get(dim, {})
        if d.get("status") == "ok":
            scores.append(w * 100)
        elif d.get("status") == "divergent":
            # Score based on severity
            if dim == "signals":
                mr = d.get("overall_match_rate", 0)
                scores.append(w * max(0, mr * 100))
            elif dim == "execution":
                mae = d.get("mae_pct", 1.0) or 1.0
                scores.append(w * max(0, (1 - mae) * 100))
            elif dim == "costs":
                mae = d.get("mae_yuan", 100.0) or 100.0
                scores.append(w * max(0, (1 - mae / 50) * 100))
            elif dim == "equity":
                div = d.get("final_divergence_pct", 100.0) or 100.0
                scores.append(w * max(0, (1 - div / 100) * 100))
        # insufficient_data: don't score

    overall = round(sum(scores)) if scores else None

    # ── Determine aggregate status ──
    statuses = [d.get("status", "ok") for d in dims.values() if d.get("status") != "insufficient_data"]
    if not statuses:
        agg_status = "insufficient_data"
    elif "divergent" in statuses:
        agg_status = "divergent"
    else:
        agg_status = "ok"

    result = {
        "status": agg_status,
        "live_date_range": [live_start, live_end],
        "dimensions": dims,
        "overall_score": overall,
        "elapsed_sec": round(time.time() - t0, 1),
    }

    # Persist
    try:
        rid = save_phase("phase8", {
            "status": result["status"],
            "live_start": live_start,
            "live_end": live_end,
            "dimensions": result["dimensions"],
            "overall_score": result["overall_score"],
            "elapsed_sec": result["elapsed_sec"],
        })
        _log.info(f"Phase 8 saved: run_id={rid}, status={agg_status}, score={overall}")
    except Exception as e:
        _log.warning(f"Phase 8 save_phase failed (non-blocking): {e}")

    return result


def get_latest_report() -> dict | None:
    """Load the most recent Phase 8 consistency report from evaluation_runs."""
    from quant.evaluation.run_store import load_latest
    return load_latest("phase8")
