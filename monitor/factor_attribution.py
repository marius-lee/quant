"""G4: 因子 PnL 归因 — 每个因子对每日组合收益的边际贡献.

方法: 因子暴露 × 因子收益率 (Barra 风格).
  因子暴露 = 组合在因子上的加权平均暴露
  因子收益率 = 标准化后的因子值 × 前向收益 → IC 即为单期因子收益率

来源: Grinold & Kahn (1999) Ch.7; Barra Risk Model Handbook.
"""
import numpy as np
import pandas as pd
from datetime import datetime
from config.constants import _require_cfg
from utils.logger import get_logger

_log = get_logger("monitor.factor_attribution")


def factor_pnl_attribution(
    positions: list[dict],
    date: str,
    store=None,
) -> dict:
    """计算当日各因子的 PnL 贡献 = 暴露 × IC (因子收益率).

    Args:
        positions: 当前持仓 [{symbol, shares, price, ...}, ...]
        date: 交易日期 YYYY-MM-DD
        store: DataStore 实例 (可选, None 则自动创建)

    Returns:
        {factor_name: {exposure, ic, contribution_bps, direction}}
        contribution_bps: 该因子贡献的 bps (1 bp = 0.01%)
    """
    if not positions:
        return {}

    from data.store import DataStore
    from factor.compute._registry import get_factor_names
    from factor.ic import compute_ic

    if store is None:
        store = DataStore()
        _close_store = True
    else:
        _close_store = False

    try:
        active_names = get_factor_names(status_filter="using")
        if not active_names:
            return {}

        symbols = [p["symbol"] for p in positions]
        if not symbols:
            return {}

        from factor.windows import max_factor_calendar_days
        eff_days = max(60, max_factor_calendar_days(active_names))
        start = (pd.Timestamp(date) - pd.Timedelta(days=eff_days)).strftime("%Y-%m-%d")
        data = store.get_daily(symbols, start=start, end=date)
        fundamentals = store.get_fundamentals(symbols, date=date)

        if data.empty:
            return {}

        from factor.compute import compute_all_factors
        factor_values = compute_all_factors(data, date, fundamentals=fundamentals, status_filter="using")
        factor_values = {k: v for k, v in factor_values.items() if isinstance(v, pd.Series)}

        if not factor_values:
            return {}

        port_weights = {}
        for p in positions:
            sym = p["symbol"]
            px = p.get("price", 0)
            shares = p.get("shares", 0)
            if px > 0 and shares > 0:
                port_weights[sym] = px * shares

        if not port_weights:
            return {}

        total_w = sum(port_weights.values())
        port_weights_norm = {s: w / total_w for s, w in port_weights.items()}

        exposures = {}
        for fname, fseries in factor_values.items():
            valid = fseries.dropna()
            common = set(valid.index) & set(port_weights_norm.keys())
            if len(common) < 3:
                continue
            exp_sum = 0.0
            w_sum = 0.0
            for sym in common:
                w = port_weights_norm.get(sym, 0)
                exp_sum += w * float(valid[sym])
                w_sum += w
            exposures[fname] = exp_sum / max(w_sum, 1e-10) if w_sum > 0 else 0.0

        if not exposures:
            return {}

        try:
            ic_result = compute_ic(factor_names=list(exposures.keys()),
                                   symbols=symbols,
                                   start=start, end=date)
            ic_map = ic_result.get("ic_map", {})
        except Exception:
            ic_map = {}

        results = {}
        for fname, exposure in sorted(exposures.items(), key=lambda x: abs(x[1]), reverse=True):
            ic_info = ic_map.get(fname, {})
            ic_mean = ic_info.get("ic_mean", 0.0) or 0.0

            if exposure > 0 and ic_mean > 0:
                direction = "✓ long winner"
            elif exposure > 0 and ic_mean < 0:
                direction = "⚠ long loser"
            elif exposure < 0 and ic_mean > 0:
                direction = "⚠ short winner"
            elif exposure < 0 and ic_mean < 0:
                direction = "✓ short loser"
            else:
                direction = "neutral"

            contribution_bps = round(abs(exposure * ic_mean) * 100, 2)

            results[fname] = {
                "exposure": round(exposure, 4),
                "ic": round(ic_mean, 4),
                "contribution_bps": contribution_bps,
                "direction": direction,
            }

        total_positive = sum(1 for r in results.values() if r["contribution_bps"] > 0.1)
        _log.info(
            f"[{date}] factor PnL attribution: {len(results)} factors, "
            f"{total_positive} positive, "
            f"top: {list(results.keys())[:5] if results else 'none'}"
        )

        return results

    finally:
        if _close_store:
            store.close()


def factor_attribution_summary(attr_result: dict) -> str:
    """将归因结果格式化为 Markdown 行."""
    if not attr_result:
        return "无因子归因数据."

    lines = ["| 因子 | 暴露 | IC | 贡献(bps) | 方向 |",
             "|------|:---:|:---:|:---:|------|"]

    for fname, info in list(attr_result.items())[:15]:
        lines.append(
            f"| {fname:30s} | {info['exposure']:+.4f} | {info['ic']:+.4f} | "
            f"{info['contribution_bps']:>6.2f} | {info['direction']} |"
        )

    return "\n".join(lines)
