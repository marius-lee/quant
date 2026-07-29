"""交易成本分析 (TCA) — P2-5.

对标 ITG/Barra: 执行后滑点 vs 到达价/VWAP 多维度成本分解。

Usage:
    from quant.execution.tca import analyze_execution
    result = analyze_execution(trades, market_data)
"""

import numpy as np
import pandas as pd
from quant.utils.logger import get_logger

_log = get_logger("execution.tca")


def analyze_execution(
    trades: list[dict],
    arrival_prices: dict[str, float] = None,
    vwap_prices: dict[str, float] = None,
) -> dict:
    """多维度交易成本分析。

    Args:
        trades: [{symbol, side, price, shares, date}, ...]
        arrival_prices: {symbol: arrival_price} — 订单到达时市价
        vwap_prices: {symbol: vwap_price} — 当日 VWAP

    Returns:
        {total_cost_bp, arrival_cost_bp, vwap_cost_bp, commission_bp,
         n_trades, breakdown: [{symbol, side, ...}], summary}
    """
    if not trades:
        return {"total_cost_bp": 0, "n_trades": 0, "summary": "no trades"}

    total_notional = 0.0
    total_arrival_cost = 0.0
    total_vwap_cost = 0.0
    total_commission = 0.0
    breakdown = []

    for t in trades:
        sym = t.get("symbol", "")
        side = t.get("side", "buy")
        px = float(t.get("price", 0))
        shares = int(t.get("shares", 0))
        notional = px * shares
        total_notional += notional

        # 佣金: 万三, 最低 5 元
        commission = max(notional * 0.0003, 5.0)
        # 印花税: 卖出万五
        stamp = notional * 0.0005 if side == "sell" else 0.0
        total_commission += commission + stamp

        arrival_cost = 0.0
        vwap_cost = 0.0

        if arrival_prices and sym in arrival_prices:
            arrival_px = arrival_prices[sym]
            if side == "buy":
                arrival_cost = (px - arrival_px) / arrival_px * notional
            else:
                arrival_cost = (arrival_px - px) / arrival_px * notional
            total_arrival_cost += arrival_cost

        if vwap_prices and sym in vwap_prices:
            vwap_px = vwap_prices[sym]
            if side == "buy":
                vwap_cost = (px - vwap_px) / vwap_px * notional
            else:
                vwap_cost = (vwap_px - px) / vwap_px * notional
            total_vwap_cost += vwap_cost

        breakdown.append({
            "symbol": sym,
            "side": side,
            "price": px,
            "shares": shares,
            "notional": round(notional, 2),
            "arrival_cost_bp": round(arrival_cost / notional * 10000, 1) if notional > 0 else 0,
            "vwap_cost_bp": round(vwap_cost / notional * 10000, 1) if notional > 0 else 0,
        })

    total_cost = total_arrival_cost + total_commission
    total_cost_bp = round(total_cost / total_notional * 10000, 1) if total_notional > 0 else 0
    arrival_cost_bp = round(total_arrival_cost / total_notional * 10000, 1) if total_notional > 0 else 0
    commission_bp = round(total_commission / total_notional * 10000, 1) if total_notional > 0 else 0
    vwap_cost_bp = round(total_vwap_cost / total_notional * 10000, 1) if total_notional > 0 else 0

    _log.info(
        f"TCA: {len(trades)} trades, total_cost={total_cost_bp}bp "
        f"(arrival={arrival_cost_bp}bp, vwap={vwap_cost_bp}bp, comm={commission_bp}bp)"
    )

    return {
        "total_cost_bp": total_cost_bp,
        "arrival_cost_bp": arrival_cost_bp,
        "vwap_cost_bp": vwap_cost_bp,
        "commission_bp": commission_bp,
        "total_notional": round(total_notional, 2),
        "n_trades": len(trades),
        "breakdown": breakdown[:20],
        "summary": (
            f"{len(trades)} trades, total {total_cost_bp}bp "
            f"(slip={arrival_cost_bp}bp comm={commission_bp}bp)"
        ),
    }
