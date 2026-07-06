"""量化选股 Pipeline — 串联 Layer 0-7, 每个交易日盘后自动运行。

每个 Layer 独立 try/except — 单层异常不中断后续层。
"""

import sys
import os
import time
from datetime import date, datetime

import numpy as np
import pandas as pd

from data.store import DataStore
from config.loader import get as cfg

from factor.compute import compute_all_factors
from factor.synth import ic_weighted, equal_weight, intersection_alpha, sleeve_compose
from risk.neutralize import neutralize

from factor.stats_cache import load_ic_map_from_cache
from risk.covariance import covariance_matrix
from risk.constraints import RiskLimits, apply_all_filters
from optimizer.portfolio import PortfolioConstructor
from optimizer.rebalance import compute_trades, validate_orders
from execution.cost import CostModel
from execution.engine import ExecutionEngine, Order
from monitor.report import generate_report, push_to_web
from config.loader import get as _ecfg
from utils.logger import get_logger

# ── HTTP state push (方案B: pipeline → Flask) ──
import uuid as _uuid

import requests as _requests, threading as _threading


# ── JSON sanitizer: convert numpy types to native Python types ──
def _sanitize_for_json(obj):
    """Recursively convert numpy types to Python native types for JSON serialization.
    Python 3.14 simplejson cannot serialize numpy.int64/float64/etc.
    """
    import numpy as _np
    if isinstance(obj, (_np.integer,)):
        return int(obj)
    if isinstance(obj, (_np.floating,)):
        return float(obj)
    if isinstance(obj, _np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(x) for x in obj]
    return obj
def _state_url() -> str:
    from config.loader import get as _cfg
    port = int(_cfg("web.port", 8521))
    return f"http://127.0.0.1:{port}/api/state"

def _post_state(data: dict, timeout: float = 5.0, max_retries: int = 3, async_mode: bool = True):
    """POST 状态到 Flask，指数退避重试。

    async_mode=True (默认): fire-and-forget 线程, 不阻塞 pipeline 步骤.
    async_mode=False: 同步模式, 用于测试/调试.
    失败静默 — 不影响 pipeline 执行.
    """
    if async_mode:
        _threading.Thread(target=_post_state_sync, args=(data, timeout, max_retries), daemon=True).start()
        return
    _post_state_sync(data, timeout, max_retries)

def _post_state_sync(data: dict, timeout: float, max_retries: int):
    """POST state to Flask. Sanitizes numpy types. Retries only on transient errors."""
    url = _state_url()
    data = _sanitize_for_json(data)
    for attempt in range(max_retries):
        try:
            r = _requests.post(url, json=data, timeout=timeout)
            if r.ok:
                return
            # 4xx client errors are permanent — do not retry
            if 400 <= r.status_code < 500:
                get_logger("pipeline").warning(f"_post_state client error {r.status_code}, not retrying")
                return
            get_logger("pipeline").warning(f"_post_state HTTP {r.status_code} (attempt {attempt+1})")
        except _requests.ConnectionError:
            # Server not running — not a transient error
            return
        except _requests.Timeout:
            get_logger("pipeline").warning(f"_post_state timeout (attempt {attempt+1})")
        except _requests.RequestException as e:
            get_logger("pipeline").warning(f"_post_state failed: {e} (attempt {attempt+1})")
        if attempt < max_retries - 1:
            import time as _time; _time.sleep(2 ** attempt)

logger = get_logger("pipeline")

LOT_SIZE = 100



def generate_signals(date_str: str = None, capital: float = None, strategy: str = "quant",
                     skip_pull: bool = False) -> dict:
    """Pipeline 阶段一: 盘前信号生成 (Steps 0-5, 不执行交易)。

    用 T-1 收盘数据计算因子 → alpha → 风险过滤 → 组合优化 → 输出目标持仓。
    返回: {date, strategy, total_capital, target_positions: [{symbol, shares, price, side}]}
    """
    if date_str is None:
        date_str = datetime.today().strftime("%Y-%m-%d")

    tid = _uuid.uuid4().hex[:12]
    from utils.logger import set_trace_id as _set_tid; _set_tid(tid)
    from monitor.metrics import metrics as _m
    _m.inc("pipeline.runs")

    t0 = time.time()
    results = {"date": date_str, "steps": {}}
    _post_state({"status": "signals_started", "progress": "0/5", "date": date_str, "trace_id": tid})
    logger.info(f"generate_signals started trace_id={tid} date={date_str}")

    # ── Step 0: Init ──
    store = DataStore()
    engine = ExecutionEngine()
    cost_model = CostModel()
    constructor = PortfolioConstructor()

    if engine.is_initialized(strategy):
        total_capital = engine.get_cash(strategy)
    else:
        seed = capital if capital is not None else 5000
        engine.set_initial_capital(strategy, seed)
        total_capital = seed

    # ── Step 1: Data Update ──
    if not skip_pull:
        try:
            n_new = store.update_daily(start="2020-01-01")
            results["steps"]["data"] = {"new_rows": n_new, "status": "ok"}
            logger.info(f"[1/5] data: {n_new} new daily rows")
            _post_state({"status": "data_synced", "progress": "1/5", "new_rows": n_new, "trace_id": tid})
            _m.inc("data.sync.rows", n_new)
        except Exception as e:
            _m.inc("pipeline.errors")
            results["steps"]["data"] = {"error": str(e), "status": "failed"}
            logger.warning(f"[1/5] data failed: {e}")
    else:
        results["steps"]["data"] = {"new_rows": 0, "status": "skipped"}

    # ── Step 2: Load ──
    try:
        conn = store._connect()
        symbols = [r[0] for r in conn.execute(
            "SELECT DISTINCT d.symbol FROM daily d JOIN stocks s ON d.symbol=s.symbol WHERE s.market!='BJ'"
        ).fetchall()]
        hist_start = (pd.Timestamp(date_str) - pd.Timedelta(days=_ecfg("data.lookback_days", 365))).strftime("%Y-%m-%d")
        data = store.get_daily(symbols, start=hist_start, end=date_str)
        fundamentals = store.get_fundamentals(symbols, date=date_str)
        results["steps"]["load"] = {"symbols": len(symbols), "status": "ok"}
        pe_cnt = int(fundamentals["pe"].notna().sum()) if "pe" in fundamentals.columns else 0
        pb_cnt = int(fundamentals["pb"].notna().sum()) if "pb" in fundamentals.columns else 0
        logger.info(f"[2/5] load: {len(symbols)} symbols, {data.shape[0]} days, PE/PB={pe_cnt}/{pb_cnt}")
        _post_state({"status": "data_loaded", "progress": "2/5", "symbols": len(symbols), "trace_id": tid})
    except Exception as e:
        _m.inc("pipeline.errors")
        results["steps"]["load"] = {"error": str(e), "status": "failed"}
        logger.warning(f"[2/5] load failed: {e}")
        store.close()
        return results

    # ── Step 3: Factor + Alpha ──
    try:
        actual_date = date_str
        if pd.Timestamp(actual_date) not in data.index:
            actual_date = data.index[-1].strftime("%Y-%m-%d")
            logger.info(f"[3/5] date adjusted: {date_str} -> {actual_date}")

        benchmark_ret = None
        try:
            bm = store.get_benchmark("000300", start="2025-12-01")
            if not bm.empty:
                benchmark_ret = bm[:pd.Timestamp(actual_date)]
        except Exception:
            pass

        factor_values = compute_all_factors(data, actual_date,
                                            fundamentals=fundamentals,
                                            benchmark_ret=benchmark_ret)
        n_valid = sum(1 for v in factor_values.values() if isinstance(v, pd.Series) and v.notna().sum() > 0)

        combine_mode = cfg("alpha.combine_mode", "sleeve")
        if combine_mode == "sleeve":
            method = 'sleeve'
            alpha_raw = sleeve_compose(
                factor_values,
                positions_per_factor=cfg("alpha.sleeve.positions_per_factor", 8),
                min_factors=cfg("alpha.sleeve.min_factors", 1),
            )
            logger.info("[3/5] sleeve: %d factors -> %d stocks", len(factor_values), alpha_raw.notna().sum())
        else:
            method = cfg("alpha.method", "ic_weighted")
            if method == "intersection":
                alpha_raw = intersection_alpha(
                    factor_values,
                    top_fraction=cfg("alpha.intersection_top_fraction", 0.20),
                    primary_factor=cfg("alpha.intersection_primary", "gap_5d"),
                )
            elif method == "ic_weighted":
                ic_map = load_ic_map_from_cache(factor_values)
                if not ic_map:
                    logger.info("IC cache unavailable, falling back to equal_weight")
                    alpha_raw = equal_weight(factor_values)
                else:
                    alpha_raw = ic_weighted(factor_values, ic_map)
            else:
                alpha_raw = equal_weight(factor_values)

        # Soft cutoff
        if method == "intersection":
            alpha = alpha_raw.copy()
        elif alpha_raw.notna().sum() > 10:
            top_frac = cfg("alpha.top_fraction", 0.30)
            if top_frac < 1.0:
                threshold = alpha_raw.quantile(1.0 - top_frac)
                below = alpha_raw < threshold
                if below.any():
                    alpha = alpha_raw.copy()
                    alpha[below] = alpha[below] * (alpha[below] / threshold) ** 2
                else:
                    alpha = alpha_raw.copy()
            else:
                alpha = alpha_raw.copy()
        else:
            alpha = alpha_raw.copy()

        results["steps"]["factor"] = {"factors": len(factor_values), "valid_stocks": alpha.dropna().count(), "status": "ok"}
        _post_state({"status": "factors_computed", "progress": "3/5", "n_factors": len(factor_values), "trace_id": tid})
        _m.gauge("factor.n_active", len(factor_values))
    except Exception as e:
        _m.inc("pipeline.errors")
        results["steps"]["factor"] = {"error": str(e), "status": "failed"}
        logger.warning(f"[3/5] factor failed: {e}")
        store.close()
        return results

    # ── Step 4: Risk ──
    try:
        close_df = data["close"]
        risk_date = actual_date if actual_date in close_df.index else close_df.index[-1].strftime("%Y-%m-%d")
        prices = close_df.loc[risk_date].dropna()
        mcap_real = fundamentals["total_mv"].reindex(prices.index)
        mcap_real = mcap_real.fillna(prices * 1e8)
        industries = fundamentals["industry"].reindex(prices.index) if "industry" in fundamentals.columns else None
        industry_min = cfg("risk.neutralization.industry_min_count", 30)
        if industries is not None and industries.notna().sum() < industry_min:
            industries = None
        alpha_neut = neutralize(alpha, industries=industries, market_caps=mcap_real)

        log_ret = np.log(close_df).diff().dropna(how="all")
        cov = covariance_matrix(log_ret, method="ledoit_wolf")

        candidates = pd.DataFrame({
            "alpha": alpha_neut, "close": prices,
            "amount": data["amount"].loc[risk_date] if risk_date in data["amount"].index
                      else data["amount"].iloc[-1]
        })
        filtered = apply_all_filters(candidates.reindex(prices.index))
        results["steps"]["risk"] = {"candidates": len(filtered), "status": "ok"}
        logger.info(f"[4/5] risk: {len(filtered)} candidates after filters")
        _post_state({"status": "risk_filtered", "progress": "4/5", "candidates": len(filtered), "trace_id": tid})
    except Exception as e:
        _m.inc("pipeline.errors")
        results["steps"]["risk"] = {"error": str(e), "status": "failed"}
        logger.warning(f"[4/5] risk failed: {e}")
        store.close()
        return results

    # ── Step 5: Optimizer (generate target positions, do NOT execute) ──
    try:
        portfolio = constructor.construct(
            filtered["alpha"], filtered["close"],
            total_capital,
        )
        # Build target positions list for the scheduler to consume
        target_positions = []
        for sym, lots in portfolio.lots.items():
            if lots > 0 and sym in prices:
                target_positions.append({
                    "symbol": sym,
                    "shares": int(lots) * LOT_SIZE,
                    "price": round(float(prices[sym]), 2),
                    "side": "buy",
                })
        results["target_positions"] = target_positions
        results["steps"]["optimizer"] = {
            "method": portfolio.method, "positions": portfolio.positions,
            "invested": round(portfolio.invested, 2), "status": "ok",
        }
        logger.info(f"[5/5] optimizer: {portfolio.method}, {portfolio.positions} pos, invested=Y{portfolio.invested:,.0f}")
        _post_state({"status": "signals_generated", "progress": "5/5",
                      "positions": portfolio.positions, "invested": portfolio.invested, "trace_id": tid})
    except Exception as e:
        _m.inc("pipeline.errors")
        results["steps"]["optimizer"] = {"error": str(e), "status": "failed"}
        logger.warning(f"[5/5] optimizer failed: {e}")
        store.close()
        return results

    store.close()
    elapsed = time.time() - t0
    results["elapsed_sec"] = round(elapsed, 1)
    logger.info(f"generate_signals done trace_id={tid} elapsed={elapsed:.1f}s date={date_str}")
    return results


def execute_signals(target_positions: list[dict], date_str: str, strategy: str = "quant") -> dict:
    """Pipeline 阶段二: 开盘执行 (Step 6)。

    对比目标持仓 vs 当前实际持仓 → 计算 delta → 执行模拟交易。
    传入的 target_positions 来自 generate_signals() 的输出。
    返回: {date, strategy, steps: {execution, monitor}}
    """
    tid = _uuid.uuid4().hex[:12]
    from utils.logger import set_trace_id as _set_tid; _set_tid(tid)
    from monitor.metrics import metrics as _m
    _m.inc("pipeline.runs")

    t0 = time.time()
    results = {"date": date_str, "steps": {}}
    logger.info(f"execute_signals started trace_id={tid} date={date_str} strategy={strategy}")

    engine = ExecutionEngine()
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

    # Load prices (use open prices for execution)
    try:
        store = DataStore()
        conn = store._connect()
        symbols = list(set(list(current_lots.keys()) + list(target_lots.keys())))
        data = store.get_daily(symbols, start=date_str, end=date_str)
        if not data.empty:
            prices = data["open"].iloc[-1] if "open" in data.columns else data["close"].iloc[-1]
        else:
            # Fallback: use cost basis for current, target price for new
            prices = {}
            for p in current_positions:
                prices[p["symbol"]] = p.get("price", 0)
            for tp in target_positions:
                if tp["symbol"] not in prices:
                    prices[tp["symbol"]] = tp["price"]
            prices = pd.Series(prices)
        store.close()
    except Exception as e:
        logger.warning(f"execute: price load failed: {e}, using cost basis")
        prices = {}
        for p in current_positions:
            prices[p["symbol"]] = p.get("price", 0)
        for tp in target_positions:
            if tp["symbol"] not in prices:
                prices[tp["symbol"]] = tp["price"]
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
    from config.loader import get as _cfg
    sl_pct = _cfg("risk.stop_loss_pct", 0.08)
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
    try:
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
        _post_state({"status": "trades_executed", "progress": "6/7", "orders": len(orders), "trace_id": tid})
        _m.inc("pipeline.trades", len(orders))
    except Exception as e:
        _m.inc("pipeline.errors")
        results["steps"]["execution"] = {"error": str(e), "status": "failed"}
        logger.warning(f"execute: execution failed: {e}")

    # ── Step 7: Monitor ──
    try:
        positions = engine.get_positions(strategy)
        trades = engine.get_trades(strategy, limit=50)
        total_wealth = engine.get_capital(strategy)
        cash_balance = engine.get_cash(strategy)
        from data.trade_repo import TradeRepo; seed = TradeRepo().get_initial_capital(strategy) or 5000
        from monitor.report import generate_report, push_to_web
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
    except Exception as e:
        results["steps"]["monitor"] = {"error": str(e), "status": "failed"}
        logger.warning(f"execute: monitor failed: {e}")

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
    return signals




if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    capital_arg = float(sys.argv[2]) if len(sys.argv) > 2 else None
    result = run(date_arg, capital_arg)
    import json
    print(json.dumps(result, indent=2, default=str))
