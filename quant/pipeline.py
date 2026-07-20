import traceback
"""量化选股 Pipeline — 串联 Layer 0-7, 每个交易日盘后自动运行。

每个 Layer 独立 try/except — 单层异常不中断后续层。
"""

import sys
import os
import time
from datetime import date, datetime

import numpy as np
import pandas as pd

from quant.data.store import DataStore

from quant.factor.compute import compute_all_factors
from quant.risk.neutralize import neutralize

from quant.factor.stats_cache import load_ic_map_from_cache
from quant.risk.covariance import covariance_matrix
from quant.risk.constraints import RiskLimits, apply_all_filters
from quant.risk.var import compute_var
from quant.optimizer.portfolio import PortfolioConstructor
from quant.optimizer.rebalance import compute_trades, validate_orders
from quant.execution.cost import CostModel
from quant.execution.engine import ExecutionEngine, Order
from quant.monitor.report import generate_report, push_to_web
from quant.config.constants import _require_cfg
from quant.core.phase_tracker import PhaseTracker, PhaseResult
from quant.utils.logger import get_logger

# ── HTTP state push (P69: 抽取到 web/state_pusher.py) ──
import uuid as _uuid
from web.state_broker import broker

logger = get_logger("pipeline")

LOT_SIZE = _require_cfg("backtest.lot_size")



def generate_signals(date_str: str = None, capital: float = None, strategy: str = "quant",
                     skip_pull: bool = False, store=None, status_filter: str = "using",
                     suppress_push: bool = False, universe_size: int = None,
                     db_path: str = "quant/data/trades.db", exclude_symbols: list = None, ic_map: dict = None, combine_mode: str = None, preloaded_data=None, primitives: dict = None, factor_store=None) -> dict:
    """Pipeline 阶段一: 盘前信号生成 (Steps 0-5, 不执行交易)。

    用 T-1 收盘数据计算因子 → alpha → 风险过滤 → 组合优化 → 输出目标持仓。
    返回: {date, strategy, total_capital, target_positions: [{symbol, shares, price, side}]}
    """
    if date_str is None:
        date_str = datetime.today().strftime("%Y-%m-%d")

    from quant.utils.logger import get_trace_id, set_trace_id as _set_tid
    tid = get_trace_id() or _uuid.uuid4().hex[:12]
    _set_tid(tid)
    from quant.monitor.metrics import metrics as _m
    _m.inc("pipeline.runs")

    t0 = time.time()
    results = {"date": date_str, "steps": {}}
    tracker = PhaseTracker("generate_signals")
    import time as _time_ph
    _ph_t0 = _time_ph.time()
    _ph_start = _time_ph.time()
    if not suppress_push:
        broker.update({"status": "signals_started", "progress": "0/5", "date": date_str, "trace_id": tid})
    logger.info(f"generate_signals started trace_id={tid} date={date_str}")

    # ── Step 0: Init ──
    _store_in = store
    store = store or DataStore()  # DataStore 始终用 quant/data/market.db
    engine = ExecutionEngine(db_path=db_path)
    cost_model = CostModel()
    constructor = PortfolioConstructor()

    from quant.data.trade_repo import TradeRepo
    seed = TradeRepo(db_path=db_path).get_initial_capital(strategy)
    if not engine.is_initialized(strategy):
        engine.set_initial_capital(strategy, seed)
    total_capital = seed  # 用初始本金, 不用剩余现金 (get_cash 会随持仓变化缩小)


    # ── Step 1: Data Update ──
    if not skip_pull:
        n_new = store.update_daily(start=_require_cfg("data.start_date"))
        results["steps"]["data"] = {"new_rows": n_new, "status": "ok"}
        logger.info(f"[1/5] data: {n_new} new daily rows")
        tracker.phases.append(PhaseResult(name="sync", started=_ph_start, finished=_time_ph.time(), status="ok"))
        _ph_start = _time_ph.time()
        if not suppress_push:
            broker.update({"status": "data_synced", "progress": "1/5", "new_rows": n_new, "trace_id": tid})
        _m.inc("data.sync.rows", n_new)
    else:
        results["steps"]["data"] = {"new_rows": 0, "status": "skipped"}

    # ── Step 2: Load ──
    from quant.data.repos import UniverseRepo
    if not universe_size:
        from quant.config.loader import get as _cfg_get
        _ucfg = _cfg_get("universe")
        symbols = UniverseRepo().get_symbols(
            exclude_market="BJ",
            exclude_st=_ucfg["exclude_st"],
            exclude_new_stock_days=_ucfg["exclude_new_stock_days"],
            min_price=_ucfg["min_price"],
            exclude_zero_turnover_days=_ucfg["exclude_zero_turnover_days"],
            min_daily_amount=_ucfg["min_daily_amount"],
        )
    else:
        symbols = UniverseRepo().get_symbols(exclude_market="BJ")
    from quant.factor.windows import max_factor_calendar_days
    _eff_days = max(_require_cfg("data.lookback_days"), max_factor_calendar_days(None))
    hist_start = (pd.Timestamp(date_str) - pd.Timedelta(days=_eff_days)).strftime("%Y-%m-%d")
    if preloaded_data is None:
        data = store.get_daily(symbols, start=hist_start, end=date_str)
    else:
        data = preloaded_data.loc[:pd.Timestamp(date_str)]
    fundamentals = store.get_fundamentals(symbols, date=date_str)
    results["steps"]["load"] = {"symbols": len(symbols), "status": "ok"}
    pe_cnt = int(fundamentals["pe"].notna().sum()) if "pe" in fundamentals.columns else 0
    pb_cnt = int(fundamentals["pb"].notna().sum()) if "pb" in fundamentals.columns else 0
    logger.info(f"[2/5] load: {len(symbols)} symbols, {data.shape[0]} days, PE/PB={pe_cnt}/{pb_cnt}")
    tracker.phases.append(PhaseResult(name="load", started=_ph_start, finished=_time_ph.time(), status="ok"))
    _ph_start = _time_ph.time()
    if not suppress_push:
        broker.update({"status": "data_loaded", "progress": "2/5", "symbols": len(symbols), "trace_id": tid})

    # ── Step 2.3: Risk pre-filters (liquidity / price / ST) → investable universe ──
    # Industry standard: risk filters applied to the ENTIRE universe BEFORE alpha scoring.
    # This replaces the old Step 4 apply_all_filters on the alpha-scored subset.
    _risk_limits = RiskLimits.from_config()
    _latest_close = data["close"].iloc[-1].dropna()
    _latest_amount = data["amount"].iloc[-1] if "amount" in data else pd.Series(dtype=float)
    _pre_df = pd.DataFrame({"close": _latest_close, "amount": _latest_amount})
    _pre_filtered = apply_all_filters(_pre_df, limits=_risk_limits, stock_names=store.get_stock_names(symbols))
    investable_symbols = _pre_filtered.index.tolist()
    logger.info(f"[2.3] risk pre-filters: {len(symbols)} → {len(investable_symbols)} investable "
                f"(liquidity>{_risk_limits.min_daily_amount}, price>{_risk_limits.min_price}, no ST)")
    # Feed investable universe into subsequent steps
    symbols = [s for s in symbols if s in set(investable_symbols)]
    data = data.loc[:, data.columns.get_level_values(1).isin(symbols)]
    fundamentals = fundamentals[fundamentals.index.isin(symbols)]
    results["steps"]["risk_pre"] = {"investable": len(symbols), "status": "ok"}
    logger.info(f"[debug] after Step 2.3: symbols={len(symbols)}")

    # ── Step 2.5: Universe size filter (backtest only) ──
    if universe_size and len(symbols) > universe_size:
        close_df = data["close"]
        latest_date = close_df.index[-1]
        latest_close = close_df.loc[latest_date].dropna()
        candidate_syms = set(latest_close.index)
        if _require_cfg("backtest.universe_filter_affordable"):
            affordable = latest_close[latest_close * LOT_SIZE <= total_capital]
            if len(affordable) > 0:
                candidate_syms &= set(affordable.index)
            # else: empty affordable pool → keep all (edge case for tiny capital)

        # ── Step 2.5b: Rank by turnover, take top N ──
        candidates = list(candidate_syms & set(symbols))
        keep_syms = store.rank_by_turnover(candidates, date_str,
                            lookback_days=_require_cfg("backtest.universe_turnover_days"),
                            top_n=universe_size)
        symbols = [s for s in symbols if s in keep_syms]
        # data is wide-format MultiIndex columns (field, symbol) — filter 2nd level
        data = data.loc[:, data.columns.get_level_values(1).isin(keep_syms)]
        fundamentals = fundamentals[fundamentals.index.isin(keep_syms)]
        results["steps"]["load"]["symbols"] = len(symbols)
        logger.info(f"[debug] after Step 2.5: symbols={len(symbols)}")


    # ── Step 2.6: Cooling-off exclude (backtest only) ──
    if exclude_symbols:
        symbols = [s for s in symbols if s not in exclude_symbols]
        data = data.loc[:, data.columns.get_level_values(1).isin(symbols)] if symbols else data.iloc[:0]
        fundamentals = fundamentals[fundamentals.index.isin(symbols)]
    # ── Step 3: Factor + Alpha ──
    actual_date = date_str
    if pd.Timestamp(actual_date) not in data.index:
        actual_date = data.index[-1].strftime("%Y-%m-%d")
        logger.info(f"[3/5] date adjusted: {date_str} -> {actual_date}")

    benchmark_ret = None
    bm = store.get_benchmark("000300", start=_require_cfg("benchmark.start_date"))
    if not bm.empty:
        benchmark_ret = bm[:pd.Timestamp(actual_date)]

    # ── ztd 预计算缓存: 确保 compute_ztd 在实盘 / 回测均能命中缓存 ──
    from quant.factor.compute.price._alternative import preload_ztd_cache
    from quant.execution.calendar import is_trading_day as _is_td
    _ztd_dates = [d for d in pd.date_range(start=pd.Timestamp(hist_start), end=pd.Timestamp(date_str), freq="B") if _is_td(d.date())]
    preload_ztd_cache([d.strftime("%Y-%m-%d") for d in _ztd_dates], symbols)

    # ── 因子值来源: factor_store (缓存) 优先, 否则实时计算 ──
    factor_values = None
    if factor_store is not None:
        try:
            factor_values = factor_store.load(actual_date, symbols=symbols, factor_names=None)
            if factor_values:
                logger.info(f"step 3: loaded {len(factor_values)} factors from factor_cache for {actual_date}")
        except Exception:
            pass

    if not len(symbols):
        logger.warning(f"[2.5] no symbols left for date={actual_date}, returning empty signals")
        return {"date": actual_date, "target_positions": [], "signal_count": 0, "steps": results["steps"]}
    if not factor_values:
        logger.info(f"step 3 starting: computing factors for {len(symbols)} symbols on {actual_date}...")
        factor_values = compute_all_factors(data, actual_date,
                                        fundamentals=fundamentals,
                                        status_filter=status_filter,
                                        benchmark_ret=benchmark_ret,
                                        primitives=primitives)
    n_valid = sum(1 for v in factor_values.values() if isinstance(v, pd.Series) and v.notna().sum() > 0)

    from quant.alpha.model import AlphaModel
    am = AlphaModel()
    ic_map = ic_map if ic_map is not None else load_ic_map_from_cache(factor_values, status_filter=status_filter)
    alpha_raw = am.combine(factor_values, ic_map=ic_map)
    alpha = am.rank(alpha_raw)

    results["_factor_values"] = {k: v for k, v in factor_values.items() if isinstance(v, pd.Series)}
    results["_alpha_raw"] = alpha_raw
    results["steps"]["factor"] = {"factors": len(factor_values), "valid_stocks": alpha.dropna().count(), "status": "ok"}
    if not suppress_push:
        broker.update({"status": "factors_computed", "progress": "3/5", "n_factors": len(factor_values), "trace_id": tid})
    _m.gauge("factor.n_active", len(factor_values))
    tracker.phases.append(PhaseResult(name="factor", started=_ph_start, finished=_time_ph.time(), status="ok"))
    _ph_start = _time_ph.time()

    # ── Step 4: Risk ──
    cov = None  # 协方差矩阵, Step 4 内计算, 供 Step 5 的 construct() 使用
    close_df = data["close"]
    risk_date = actual_date if actual_date in close_df.index else close_df.index[-1].strftime("%Y-%m-%d")
    prices = close_df.loc[risk_date].dropna()
    mcap_real = fundamentals["total_mv"].reindex(prices.index)
    mcap_real = mcap_real.fillna(prices * 1e8)
    industries = fundamentals["industry"].reindex(prices.index) if "industry" in fundamentals.columns else None
    industry_min = _require_cfg("risk.neutralize.min_common_stocks")
    if industries is not None and industries.notna().sum() < industry_min:
        industries = None
    alpha_neut = neutralize(alpha, industries=industries, market_caps=mcap_real)

    log_ret = np.log(close_df).diff().dropna(how="all")
    cov = covariance_matrix(log_ret, method="ledoit_wolf")

    # Step 4 candidates: alpha_neut already within investable universe (pre-filtered in Step 2.3)
    # Risk pre-filters (liquidity/price/ST) are already applied — no re-filtering here.
    # Only stocks with valid alpha scores pass through to the optimizer.
    candidates = pd.DataFrame({
        "alpha": alpha_neut, "close": prices,
    })
    # Drop rows where alpha is NaN (stocks not scored by alpha model)
    filtered = candidates.dropna(subset=["alpha"])
    results["steps"]["risk"] = {"candidates": len(filtered), "status": "ok"}
    logger.info(f"[4/5] risk: {len(filtered)} candidates after filters")
    tracker.phases.append(PhaseResult(name="risk", started=_ph_start, finished=_time_ph.time(), status="ok"))
    _ph_start = _time_ph.time()

    # VaR risk budget check: warn if portfolio VaR exceeds ~3% of exposure
    try:
        if cov is not None and len(filtered) > 0 and "close" in candidates.columns:
            _v = candidates["close"].dropna().iloc[:min(10, len(filtered))]
            _exposure = float(_v.sum()) * LOT_SIZE if len(_v) > 0 else 0
            if _exposure > 0:
                _w = pd.Series(1.0 / max(len(_v), 1), index=_v.index)
                _var = compute_var(_exposure, _w, cov, confidence=0.95)
                if _var and abs(_var / _exposure) > 0.03:
                    logger.warning("[4/5] VaR warning: daily VaR=%.1f (%.1f%% of exposure)",
                                   abs(_var), abs(_var / _exposure) * 100)
    except Exception:
        pass
    if not suppress_push:
        broker.update({"status": "risk_filtered", "progress": "4/5", "candidates": len(filtered), "trace_id": tid})

    # ── Step 5: Optimizer (generate target positions, do NOT execute) ──
    portfolio = constructor.construct(
        filtered["alpha"], filtered["close"],
        total_capital,
        covariance=cov, ic_map=ic_map,
    )
    # Build target positions list for the scheduler to consume
    target_positions = []
    for sym, lots in portfolio.lots.items():
        if lots > 0 and sym in prices:
            score = round(float(alpha_neut.get(sym, 0)), 4)
            target_positions.append({
                "symbol": sym,
                "score": score if not (isinstance(score, float) and score != score) else 0.0,
                "shares": int(lots) * LOT_SIZE,
                "price": round(float(prices[sym]), 2),
                "side": "buy",
                "industry": str(industries.get(sym, "")) if (industries is not None and not (isinstance(industries.get(sym, ""), float) and industries.get(sym, "") != industries.get(sym, ""))) else "",
            })
    # ── rank by score descending, annotate reason ──
    target_positions.sort(key=lambda x: x.get("score", 0), reverse=True)
    for i, tp in enumerate(target_positions):
        tp["reason"] = f"#{i+1}"
    results["target_positions"] = target_positions
    results["steps"]["optimizer"] = {
        "method": portfolio.method, "positions": portfolio.positions,
        "invested": round(portfolio.invested, 2), "status": "ok",
    }
    logger.info(f"[5/5] optimizer: {portfolio.method}, {portfolio.positions} pos, invested=Y{portfolio.invested:,.0f}")
    if not suppress_push:
        broker.update({"status": "signals_generated", "progress": "5/5",
                    "n_positions": portfolio.positions, "invested": portfolio.invested, "trace_id": tid, "signals": target_positions})

    if _store_in is None:
        store.close()
    elapsed = time.time() - t0
    # Persist to daily_signals — every caller (scheduler, web, CLI) gets DB-backed signals
    targets = results.get("target_positions", [])
    if targets:
        from quant.data.trade_repo import TradeRepo
        TradeRepo(db_path=db_path).save_signals(date_str, targets, total_capital, strategy)
        logger.info(f"[pipeline] saved {len(targets)} targets to daily_signals for {date_str}")

    results["elapsed_sec"] = round(elapsed, 1)
    logger.info(f"generate_signals done trace_id={tid} elapsed={elapsed:.1f}s phases=[{tracker.summary()}] date={date_str}")
    return results


def execute_signals(target_positions: list[dict], date_str: str, strategy: str = "quant",
                    prices: dict = None, db_path: str = "quant/data/trades.db",
                    suppress_push: bool = False) -> dict:
    """Pipeline 阶段二: 开盘执行 (Step 6)。

    prices: 预提供的开盘价dict (回测用); None则由fetch_quotes获取实时报价.
    db_path: 交易数据库路径 (回测用); None使用默认.
    suppress_push: True→不调用 broker.update (回测用).
    """
    from quant.utils.logger import get_trace_id, set_trace_id as _set_tid
    tid = get_trace_id() or _uuid.uuid4().hex[:12]
    _set_tid(tid)
    from quant.monitor.metrics import metrics as _m
    _m.inc("pipeline.runs")

    t0 = time.time()
    results = {"date": date_str, "steps": {}}
    tracker = PhaseTracker("generate_signals")
    import time as _time_ph
    _ph_t0 = _time_ph.time()
    _ph_start = _time_ph.time()
    logger.info(f"execute_signals started trace_id={tid} date={date_str} strategy={strategy}")

    engine = ExecutionEngine(db_path=db_path)
    cost_model = CostModel()

    # Get current positions
    current_positions = engine.get_positions(strategy)
    logger.info(f"execute: {len(current_positions)} current positions, {len(target_positions)} target")

    # Build current lots map
    current_lots = {}
    for p in current_positions:
        current_lots[p["symbol"]] = p["shares"] // LOT_SIZE

    # Build target lots map
    target_lots = {}
    for tp in target_positions:
        sym = tp["symbol"]
        target_lots[sym] = tp["shares"] // LOT_SIZE

    # Load prices — 直接用 Sina 实时开盘价, 不走 market.db 回退.
    if prices is not None:
        # Backtest mode: use provided open prices directly
        prices = pd.Series(prices)
    else:
        # Live mode: fetch from Sina
        # 拉不到报价 → 不执行 (用错价格比不交易危害大, 且永不 fallback 制造隐形 bug).
        from quant.execution.quote import fetch_quotes
        symbols = list(set(list(current_lots.keys()) + list(target_lots.keys())))
        quotes = fetch_quotes(symbols)
        if not quotes:
            logger.error(
                f"execute: fetch_quotes returned empty for {len(symbols)} symbols — "
                f"skipping execution to avoid trading at stale prices"
            )
            return results

        prices = {}
        for sym, q in quotes.items():
            open_px = q.get("open", 0)
            if open_px > 0:
                prices[sym] = open_px
        # 报价未覆盖的持仓保留成本价 (仅用于估值, 不用于新买入)
        for p in current_positions:
            if p["symbol"] not in prices:
                prices[p["symbol"]] = p.get("price", 0)
        # 报价未覆盖的目标 (极罕见) 使用 sina price 而非昨日 close
        for tp in target_positions:
            if tp["symbol"] not in prices:
                q = quotes.get(tp["symbol"], {})
                prices[tp["symbol"]] = q.get("price", 0) or q.get("open", 0)
        prices = pd.Series(prices)
    prices = pd.Series(prices)

    # Compute total capital
    cash = engine.get_cash(strategy)
    position_value = 0.0
    for p in current_positions:
        px = prices.get(p["symbol"], p.get("price", 0))
        if pd.isna(px) or px <= 0:
            px = p.get("price", 0)
        position_value += p["shares"] * float(px)
    total_capital = round(cash + position_value, 2)

    # ── Stop-Loss check ──
    sl_pct = _require_cfg("risk.stop_loss_pct")
    for p in current_positions:
        cost_basis = p.get("price", 0)
        current_px = prices.get(p["symbol"], None)
        if current_px is None or current_px <= 0 or cost_basis <= 0 or pd.isna(current_px):
            continue
        drop = (float(current_px) - cost_basis) / cost_basis
        if drop <= -sl_pct:
            shares = int(p["shares"])
            if shares > 0:
                logger.warning(f"[SL] execute stop-loss: {p['symbol']} drop={drop:.1%}, selling {shares}")
                engine.execute(
                    [Order(symbol=p["symbol"], side="sell", shares=shares, price=float(current_px), cost=0)],
                    date_str, strategy)
        current_positions = engine.get_positions(strategy)
        current_lots = {p2["symbol"]: p2["shares"] // LOT_SIZE for p2 in current_positions}

    # ── Compute trades (delta) ──
    current_lots_series = pd.Series(current_lots, dtype=int)
    target_lots_series = pd.Series(target_lots, dtype=int)
    orders = compute_trades(
        target_lots_series, current_lots_series, prices, cost_model,
        capital=total_capital, cash=engine.get_cash(strategy),
    )
    if orders:
        is_valid, msg = validate_orders(orders, engine.get_cash(strategy))
        if not is_valid:
            logger.warning(f"execute: validate_orders failed: {msg}, skipping")
            orders = []
        else:
            engine.execute(orders, date_str, strategy)

    results["steps"]["execution"] = {
        "orders": len(orders),
        "buys": sum(1 for o in orders if o.side == "buy"),
        "sells": sum(1 for o in orders if o.side == "sell"),
        "status": "ok",
    }
    logger.info(f"execute: {len(orders)} orders ({results['steps']['execution']['buys']} buys, {results['steps']['execution']['sells']} sells)")
    if not suppress_push:
        broker.update({"status": "trades_executed", "progress": "6/7", "orders": len(orders), "trace_id": tid, "signals": target_positions})
    _m.inc("pipeline.trades", len(orders))

    # ── Step 7: Monitor ──
    positions = engine.get_positions(strategy)
    trades = engine.get_trades(strategy, limit=50)
    total_wealth = engine.get_capital(strategy)
    cash_balance = engine.get_cash(strategy)
    from quant.data.trade_repo import TradeRepo
    seed = TradeRepo(db_path=db_path).get_initial_capital(strategy)
    from quant.monitor.report import generate_report, push_to_web
    report = generate_report(
        date_str, cash_balance, positions, trades,
        pnl_total=total_wealth - seed,
        initial_capital=seed,
    )
    push_to_web(report)
    cap = report["capital"]
    results["steps"]["monitor"] = {
        "cash": cap["cash"], "positions_value": cap["positions_value"],
        "total_wealth": cap["total_wealth"],
        "total_return": report["metrics"]["total_return_pct"], "status": "ok",
    }
    logger.info(f"execute monitor: wealth=Y{cap['total_wealth']:,.2f} return={report['metrics']['total_return_pct']}%")

    elapsed = time.time() - t0
    results["elapsed_sec"] = round(elapsed, 1)
    logger.info(f"execute_signals done trace_id={tid} elapsed={elapsed:.1f}s")
    return results





def run(date_str: str = None, capital: float = None, strategy: str = "quant", skip_pull: bool = False):
    """完整 Pipeline（向后兼容包装器）。

    阶段一: generate_signals() → 目标持仓
    阶段二: execute_signals() → 执行交易
    """
    signals = generate_signals(date_str, capital, strategy, skip_pull)
    if "target_positions" not in signals:
        logger.warning("generate_signals returned no target positions, skipping execution")
        return signals

    exec_result = execute_signals(signals["target_positions"], signals["date"], strategy)
    # Merge steps
    signals["steps"].update(exec_result.get("steps", {}))
    signals["elapsed_sec"] = signals.get("elapsed_sec", 0) + exec_result.get("elapsed_sec", 0)
    signals["stopped_out"] = exec_result.get("stopped_out", [])
# ── Trace recording (non-blocking) ──
    try:
        from quant.core.trace import get_trace, make_experiment, Hypothesis, ExperimentFeedback
        trace = get_trace()
        exp = make_experiment(
            action="pipeline_run",
            hypothesis=Hypothesis(
                hypothesis=f"Strategy {strategy} generates excess returns",
                reason=f"Pipeline run: factor eval + execution",
                source="pipeline.run()",
            ),
        )
        steps = signals.get("steps", {})
        exp.sub_results = {"date": signals.get("date"), "elapsed_sec": signals.get("elapsed_sec", 0)}
        exp.sub_results["steps_summary"] = {k: {sk: sv for sk, sv in v.items() if sk != "status"}
                                            for k, v in steps.items()}
        total_return = steps.get("monitor", {}).get("total_return", 0)
        exp.feedback = ExperimentFeedback(
            decision=total_return > 0,
            reason=f"Pipeline completed. Return: {total_return}",
            metrics={"total_return_pct": float(total_return) if total_return else 0.0},
        )
        trace.record(exp)
    except Exception as _e:
        logger.warning(f"Trace recording failed (non-blocking): {_e}")
    return signals




if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    capital_arg = float(sys.argv[2]) if len(sys.argv) > 2 else None
    result = run(date_arg, capital_arg)
    import json
    print(json.dumps(result, indent=2, default=str))
