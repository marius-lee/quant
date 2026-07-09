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

# ── HTTP state push (P69: 抽取到 web/state_pusher.py) ──
import uuid as _uuid
from web.state_pusher import post_state

logger = get_logger("pipeline")

LOT_SIZE = _ecfg("backtest.lot_size")



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
    post_state({"status": "signals_started", "progress": "0/5", "date": date_str, "trace_id": tid})
    logger.info(f"generate_signals started trace_id={tid} date={date_str}")

    # ── Step 0: Init ──
    store = DataStore()
    engine = ExecutionEngine()
    cost_model = CostModel()
    constructor = PortfolioConstructor()

    if engine.is_initialized(strategy):
        total_capital = engine.get_cash(strategy)
    else:
        seed = capital
        engine.set_initial_capital(strategy, seed)
        total_capital = seed

    # ── Step 1: Data Update ──
    if not skip_pull:
        try:
            n_new = store.update_daily(start="2020-01-01")
            results["steps"]["data"] = {"new_rows": n_new, "status": "ok"}
            logger.info(f"[1/5] data: {n_new} new daily rows")
            post_state({"status": "data_synced", "progress": "1/5", "new_rows": n_new, "trace_id": tid})
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
        post_state({"status": "data_loaded", "progress": "2/5", "symbols": len(symbols), "trace_id": tid})
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

        from utils.logger import get_logger
        _flog = get_logger("factor.compute")
        _flog.info(f"step 3 starting: computing factors for {len(symbols)} symbols on {actual_date}...")
        factor_values = compute_all_factors(data, actual_date,
                                            fundamentals=fundamentals,
                                            benchmark_ret=benchmark_ret)
        n_valid = sum(1 for v in factor_values.values() if isinstance(v, pd.Series) and v.notna().sum() > 0)

        from alpha.model import AlphaModel
        am = AlphaModel()
        ic_map = load_ic_map_from_cache(factor_values)
        alpha_raw = am.combine(factor_values, ic_map=ic_map)
        alpha = am.rank(alpha_raw)

        results["steps"]["factor"] = {"factors": len(factor_values), "valid_stocks": alpha.dropna().count(), "status": "ok"}
        post_state({"status": "factors_computed", "progress": "3/5", "n_factors": len(factor_values), "trace_id": tid})
        _m.gauge("factor.n_active", len(factor_values))
    except Exception as e:
        _m.inc("pipeline.errors")
        results["steps"]["factor"] = {"error": str(e), "status": "failed"}
        logger.warning(f"[3/5] factor failed: {e}")
        store.close()
        return results

    # ── Step 4: Risk ──
    cov = None  # 协方差矩阵, Step 4 内计算, 供 Step 5 的 construct() 使用
    try:
        close_df = data["close"]
        risk_date = actual_date if actual_date in close_df.index else close_df.index[-1].strftime("%Y-%m-%d")
        prices = close_df.loc[risk_date].dropna()
        mcap_real = fundamentals["total_mv"].reindex(prices.index)
        mcap_real = mcap_real.fillna(prices * 1e8)
        industries = fundamentals["industry"].reindex(prices.index) if "industry" in fundamentals.columns else None
        industry_min = cfg("risk.neutralize.min_common_stocks")
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
        post_state({"status": "risk_filtered", "progress": "4/5", "candidates": len(filtered), "trace_id": tid})
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
            covariance=cov,
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
        post_state({"status": "signals_generated", "progress": "5/5",
                      "n_positions": portfolio.positions, "invested": portfolio.invested, "trace_id": tid, "signals": target_positions}, async_mode=False)
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

    # Load prices — 直接用 Sina 实时开盘价, 不走 market.db 回退.
    # 09:30 开盘后 open 价格已锁定, 用集合竞价确定的开盘价执行买入更接近真实成交.
    # 拉不到报价 → 不执行 (用错价格比不交易危害大, 且永不 fallback 制造隐形 bug).
    from execution.quote import fetch_quotes
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
    sl_pct = _cfg("risk.stop_loss_pct")
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
        post_state({"status": "trades_executed", "progress": "6/7", "orders": len(orders), "trace_id": tid, "signals": target_positions}, async_mode=False)
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
        from data.trade_repo import TradeRepo; seed = TradeRepo().get_initial_capital(strategy)
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
