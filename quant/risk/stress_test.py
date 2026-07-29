"""Stress Testing — 历史场景重放 + 敏感性分析 (P1-③, test-v262).

对标 Barra / MSCI 压力测试框架:
  - 历史极端事件重放: 2015股灾/2020疫情/2022封城
  - 敏感性分析: 单因子 ±2σ 冲击 → 组合 VaR 变化

Usage:
    from quant.risk.stress_test import run_stress_tests
    result = run_stress_tests(positions, capital)
"""

import numpy as np
import pandas as pd
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

_log = get_logger("risk.stress_test")

# ═══════════════════════════════════════════════════════════
# 历史极端事件 (A 股标志性危机)
# ═══════════════════════════════════════════════════════════

HISTORICAL_SCENARIOS = {
    "2015股灾": {
        "start": "2015-06-12", "end": "2015-07-08",
        "description": "上证5178→3500, 千股跌停, 去杠杆",
    },
    "2016熔断": {
        "start": "2016-01-04", "end": "2016-01-07",
        "description": "熔断机制首日触发, 两次提前收市, 千股跌停",
    },
    "2018贸易战": {
        "start": "2018-03-22", "end": "2018-10-18",
        "description": "中美贸易摩擦升级, 上证-25%",
    },
    "2020疫情": {
        "start": "2020-01-23", "end": "2020-02-03",
        "description": "武汉封城, 节后暴跌 -7.7%, 3200只跌停",
    },
    "2022封城": {
        "start": "2022-03-14", "end": "2022-03-15",
        "description": "上海封城 + 中概股退市恐慌, 恒生科技 -11%",
    },
    "2024年初": {
        "start": "2024-01-29", "end": "2024-02-05",
        "description": "雪球敲入 + 量化DMA爆仓, 千股跌停再现",
    },
}


def run_stress_tests(
    positions: list[dict],
    capital: float,
    scenarios: dict = None,
) -> dict:
    """对当前持仓运行压力测试。

    Args:
        positions: [{symbol, shares, price}, ...]
        capital: 总资产
        scenarios: 自定义场景 (None=用 HISTORICAL_SCENARIOS)

    Returns:
        {scenario_name: {index_return_pct, portfolio_loss_est, loss_pct, description}}
    """
    if scenarios is None:
        scenarios = HISTORICAL_SCENARIOS

    if not positions:
        return {"status": "no positions", "scenarios": {}}

    total_value = sum(p.get("value", p["shares"] * p.get("price", 0)) for p in positions)
    if total_value <= 0:
        return {"status": "zero value", "scenarios": {}}

    import sqlite3
    from quant.config.paths import MARKET_DB

    results = {}
    conn = sqlite3.connect(MARKET_DB)

    for name, sc in scenarios.items():
        # 查场景区间内 CSI 300 跌幅
        row = conn.execute(
            "SELECT MIN(close), MAX(close), "
            "(SELECT close FROM benchmark_daily WHERE index_code='000300' AND date<=? ORDER BY date DESC LIMIT 1) as start_close, "
            "(SELECT close FROM benchmark_daily WHERE index_code='000300' AND date<=? ORDER BY date DESC LIMIT 1) as end_close "
            "FROM benchmark_daily WHERE index_code='000300' AND date>=? AND date<=?",
            (sc["start"], sc["end"], sc["start"], sc["end"])
        ).fetchone()

        if not row or not row[2] or not row[3]:
            results[name] = {"index_return_pct": 0, "portfolio_loss_est": 0,
                             "loss_pct": 0, "description": sc["description"],
                             "error": "no benchmark data"}
            continue

        idx_return = (row[3] - row[2]) / row[2] * 100

        # 估算组合损失: 假设持仓 beta≈1, 损失 ≈ 仓位 × 指数跌幅
        symbols = [p["symbol"] for p in positions]
        ph = ",".join("?" * len(symbols))
        stock_returns = {}
        for sym in symbols:
            sr = conn.execute(
                f"SELECT close FROM daily WHERE symbol=? AND date>=? AND date<=? ORDER BY date LIMIT 1",
                (sym, sc["start"], sc["end"])
            ).fetchone()
            er = conn.execute(
                f"SELECT close FROM daily WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",
                (sym, sc["end"])
            ).fetchone()
            if sr and er and sr[0] > 0:
                stock_returns[sym] = (er[0] - sr[0]) / sr[0] * 100

        # 加权平均个股跌幅
        if stock_returns:
            wtd = sum(
                (p.get("value", p["shares"] * p.get("price", 0)) / total_value)
                * stock_returns.get(p["symbol"], idx_return)
                for p in positions
            )
        else:
            wtd = idx_return  # fallback: 指数跌幅

        loss_est = total_value * abs(wtd) / 100
        results[name] = {
            "index_return_pct": round(idx_return, 2),
            "portfolio_loss_est": round(loss_est, 2),
            "loss_pct": round(abs(wtd), 2),
            "description": sc["description"],
        }

    conn.close()
    _log.info(f"stress test: {len(results)} scenarios, "
              f"worst loss={max((r['loss_pct'] for r in results.values()), default=0):.1f}%")
    return {"status": "ok", "scenarios": results, "capital": capital}


def sensitivity_analysis(
    positions: list[dict],
    capital: float,
    shock_pct: float = 0.05,
) -> dict:
    """敏感性分析: 单因子 ±2σ 冲击对组合的影响。

    简化为: 假设组合 beta≈1, 冲击 = 指数×shock_pct。
    返回冲击后 VaR 变化。
    """
    if not positions:
        return {"impact": 0, "scenario": "no positions"}

    pos_value = sum(p.get("value", p["shares"] * p.get("price", 0)) for p in positions)
    impact = pos_value * shock_pct
    return {
        "scenario": f"指数 ±{shock_pct*100:.0f}% 冲击",
        "position_value": round(pos_value, 2),
        "impact_amount": round(impact, 2),
        "impact_pct": round(shock_pct * 100, 1),
        "new_nav_lower": round(capital - impact, 2),
        "circuit_breaker": impact / capital > 0.10,
    }
