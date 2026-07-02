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
from data.trade_repo import TradeRepo
from factor.compute import compute_all_factors
from factor.synth import equal_weight
from alpha.model import AlphaModel
from risk.neutralize import neutralize
from risk.covariance import covariance_matrix
from risk.constraints import RiskLimits, apply_all_filters
from optimizer.portfolio import PortfolioConstructor
from optimizer.rebalance import compute_trades
from execution.cost import CostModel
from execution.engine import ExecutionEngine
from monitor.report import generate_report, push_to_web
from utils.logger import get_logger

logger = get_logger("pipeline")

LOT_SIZE = 100


def run(date_str: str = None, capital: float = None, strategy: str = "quant"):
    """执行完整 Pipeline。

    date_str: 交易日期 (YYYY-MM-DD), None = 最近一个交易日
    capital: 本金, None = 从 strategy_config / config 读取
    strategy: 策略名前缀
    """
    if date_str is None:
        date_str = datetime.today().strftime("%Y-%m-%d")

    if capital is None:
        from config.loader import get as cfg
        capital = cfg("backtest.initial_capital", 5000)

    t0 = time.time()
    results = {"date": date_str, "steps": {}}

    # ── Step 0: Init ──
    store = DataStore()
    repo = TradeRepo()
    engine = ExecutionEngine()
    cost_model = CostModel()
    constructor = PortfolioConstructor()
    alpha_model = AlphaModel(method="equal_weight")

    # ── Step 1: Data Update ──
    try:
        n_new = store.update_daily(start="2020-01-01")
        results["steps"]["data"] = {"new_rows": n_new, "status": "ok"}
        logger.info(f"[1/7] data: {n_new} new daily rows")
    except Exception as e:
        results["steps"]["data"] = {"error": str(e), "status": "failed"}
        logger.warning(f"[1/7] data failed: {e}")

    # ── Step 2: Get symbols + data ──
    try:
        conn = store._connect()
        symbols = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM daily WHERE date >= ? ORDER BY date DESC LIMIT 1000",
            (date_str,)
        ).fetchall()]
        if not symbols:
            symbols = [r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM daily ORDER BY date DESC LIMIT 500"
            ).fetchall()]
        # Get enough history for factor computation (need 60+ days)
        data = store.get_daily(symbols, start="2026-01-01", end=date_str)
        # 基本面数据 — 用于价值因子计算和市值中性化
        fundamentals = store.get_fundamentals(symbols)
        results["steps"]["load"] = {
            "symbols": len(symbols),
            "fund_pe_valid": int(fundamentals["pe"].notna().sum()),
            "fund_pb_valid": int(fundamentals["pb"].notna().sum()),
            "status": "ok",
        }
        logger.info(f"[2/7] load: {len(symbols)} symbols, {data.shape[0]} days, fundamentals: PE/PB={fundamentals['pe'].notna().sum()}/{fundamentals['pb'].notna().sum()}")
    except Exception as e:
        results["steps"]["load"] = {"error": str(e), "status": "failed"}
        logger.warning(f"[2/7] load failed: {e}")
        return results

    # ── Step 3: Factor + Alpha ──
    try:
        # 使用数据中实际存在的最新日期 (避免 date_str 不在 data.index 中)
        actual_date = date_str
        if actual_date not in data.index:
            actual_date = data.index[-1].strftime("%Y-%m-%d")
            logger.info(f"[3/7] date adjusted: {date_str} → {actual_date}")
        factor_values = compute_all_factors(data, actual_date, fundamentals=fundamentals)
        alpha = equal_weight(factor_values)
        results["steps"]["factor"] = {
            "factors": len(factor_values),
            "valid_stocks": alpha.dropna().count(),
            "status": "ok",
        }
        logger.info(f"[3/7] factor: {len(factor_values)} factors, {alpha.dropna().count()} stocks")
    except Exception as e:
        results["steps"]["factor"] = {"error": str(e), "status": "failed"}
        logger.warning(f"[3/7] factor failed: {e}")
        return results

    # ── Step 4: Risk ──
    try:
        close_df = data["close"]
        risk_date = actual_date if actual_date in close_df.index else close_df.index[-1].strftime("%Y-%m-%d")
        prices = close_df.loc[risk_date].dropna()
        # 用 fundamentals 中的真实总市值做中性化 (缺失值回退到 price × 1e8 估算)
        mcap_real = fundamentals["total_mv"].reindex(prices.index)
        mcap_real = mcap_real.fillna(prices * 1e8)
        alpha_neut = neutralize(alpha, market_caps=mcap_real)

        log_ret = np.log(close_df).diff().dropna(how="all")
        cov = covariance_matrix(log_ret, method="ledoit_wolf", window=60)

        candidates = pd.DataFrame({
            "alpha": alpha_neut, "close": prices, "amount": data["amount"].iloc[-5:].mean()
        })
        filtered = apply_all_filters(candidates.reindex(prices.index))
        results["steps"]["risk"] = {
            "candidates": len(filtered),
            "cov_shape": list(cov.shape),
            "status": "ok",
        }
        logger.info(f"[4/7] risk: {len(filtered)} candidates after filters")
    except Exception as e:
        results["steps"]["risk"] = {"error": str(e), "status": "failed"}
        logger.warning(f"[4/7] risk failed: {e}")
        return results

    # ── Step 5: Optimizer ──
    try:
        current_capital = engine.get_capital(strategy)
        portfolio = constructor.construct(filtered["alpha"], filtered["close"], current_capital)
        results["steps"]["optimizer"] = {
            "method": portfolio.method,
            "positions": portfolio.positions,
            "invested": round(portfolio.invested, 2),
            "status": "ok",
        }
        logger.info(f"[5/7] optimizer: {portfolio.method}, {portfolio.positions} pos, invested=¥{portfolio.invested:,.0f}")
    except Exception as e:
        results["steps"]["optimizer"] = {"error": str(e), "status": "failed"}
        logger.warning(f"[5/7] optimizer failed: {e}")
        return results

    # ── Step 6: Execution ──
    try:
        current_positions = engine.get_positions(strategy)
        current_lots = pd.Series({p["symbol"]: p["shares"] // LOT_SIZE for p in current_positions}, dtype=int)
        current_capital = engine.get_capital(strategy)

        orders = compute_trades(
            portfolio.lots, current_lots, prices, cost_model, capital=current_capital
        )
        if orders:
            engine.execute(orders, date_str, strategy)

        results["steps"]["execution"] = {
            "orders": len(orders),
            "buys": sum(1 for o in orders if o.side == "buy"),
            "sells": sum(1 for o in orders if o.side == "sell"),
            "status": "ok",
        }
        logger.info(f"[6/7] execution: {len(orders)} orders")
    except Exception as e:
        results["steps"]["execution"] = {"error": str(e), "status": "failed"}
        logger.warning(f"[6/7] execution failed: {e}")

    # ── Step 7: Monitor ──
    try:
        positions = engine.get_positions(strategy)
        trades = engine.get_trades(strategy, limit=50)
        # get_capital() = total wealth (cash + positions_value)
        # get_cash() = cash only → correct for generate_report's capital param
        total_wealth = engine.get_capital(strategy)
        cash_balance = engine.get_cash(strategy)

        report = generate_report(
            date_str, cash_balance, positions, trades,
            pnl_total=total_wealth - capital,
            initial_capital=capital,
        )
        push_to_web(report)
        cap = report["capital"]
        results["steps"]["monitor"] = {
            "cash": cap["cash"],
            "positions_value": cap["positions_value"],
            "total_wealth": cap["total_wealth"],
            "total_return": report["metrics"]["total_return_pct"],
            "status": "ok",
        }
        logger.info(f"[7/7] monitor: wealth=¥{cap['total_wealth']:,.2f} (cash=¥{cap['cash']:,.2f} + pos=¥{cap['positions_value']:,.2f}), return={report['metrics']['total_return_pct']}%")
    except Exception as e:
        results["steps"]["monitor"] = {"error": str(e), "status": "failed"}
        logger.warning(f"[7/7] monitor failed: {e}")

    store.close()
    elapsed = time.time() - t0
    results["elapsed_sec"] = round(elapsed, 1)
    logger.info(f"pipeline done in {elapsed:.1f}s — {date_str}")
    return results


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    capital_arg = float(sys.argv[2]) if len(sys.argv) > 2 else None
    result = run(date_arg, capital_arg)
    import json
    print(json.dumps(result, indent=2, default=str))
