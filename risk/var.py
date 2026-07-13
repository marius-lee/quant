"""Gap 6: Risk management — VaR/CVaR + stress testing + correlation breakdown.

Parametric VaR using existing Ledoit-Wolf covariance matrix.
Historical scenario replay using benchmark_daily stress periods.
Correlation stress test for diversification failure.
"""

import numpy as np
import pandas as pd
import os, sqlite3, json
from scipy.stats import norm
from utils.logger import get_logger

_log = get_logger("risk.var")

_MARKET_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")

# Stress scenarios (CSI 300 worst drawdown periods)
STRESS_SCENARIOS = {
    "2015_gu_zai": ("2015-06-12", "2015-07-08", "2015 股灾 (-32%)"),
    "2018_trade_war": ("2018-01-26", "2018-10-18", "2018 贸易战 (-29%)"),
    "2020_covid": ("2020-01-20", "2020-02-03", "2020 COVID (-12%)"),
    "2024_liquidity": ("2024-01-29", "2024-02-05", "2024 流动性危机 (-8%)"),
}


def compute_var(portfolio_value, weights, cov_matrix, confidence=0.95):
    """Parametric VaR: loss that won't be exceeded with given confidence.

    VaR_95 = portfolio_value * sigma_portfolio * norm.ppf(0.95)
    sigma_portfolio = sqrt(w^T @ Sigma @ w)
    """
    if cov_matrix is None or weights.empty:
        return None
    w = weights.values if hasattr(weights, 'values') else np.array(list(weights.values()))
    Sigma = cov_matrix.values if hasattr(cov_matrix, 'values') else np.array(cov_matrix)
    common = min(len(w), Sigma.shape[0])
    w = w[:common]
    Sigma = Sigma[:common, :common]
    port_var = w.T @ Sigma @ w
    if port_var <= 0:
        return None
    port_sigma = np.sqrt(port_var)
    z = norm.ppf(confidence)
    return portfolio_value * port_sigma * z


def compute_cvar(portfolio_value, weights, cov_matrix, confidence=0.95):
    """Parametric CVaR (Expected Shortfall).

    CVaR = portfolio_value * sigma * phi(z_alpha) / (1 - alpha)
    where phi is standard normal PDF.
    """
    if cov_matrix is None or weights.empty:
        return None
    w = weights.values if hasattr(weights, 'values') else np.array(list(weights.values()))
    Sigma = cov_matrix.values if hasattr(cov_matrix, 'values') else np.array(cov_matrix)
    common = min(len(w), Sigma.shape[0])
    w = w[:common]
    Sigma = Sigma[:common, :common]
    port_var = w.T @ Sigma @ w
    if port_var <= 0:
        return None
    port_sigma = np.sqrt(port_var)
    z = norm.ppf(confidence)
    phi_z = norm.pdf(z)
    cvar = phi_z / (1 - confidence)
    return portfolio_value * port_sigma * cvar


def stress_test(positions, weights):
    """Historical scenario replay: what if a historical crash happened today?

    Returns: dict[scenario_name] = estimated loss (RMB)
    """
    try:
        conn = sqlite3.connect(_MARKET_DB)
        syms = [p["symbol"] for p in positions] if isinstance(positions, list) else positions
        placeholders = ",".join("?" * len(syms))
        results = {}
        for name, (start_d, end_d, label) in STRESS_SCENARIOS.items():
            rows = conn.execute(
                f"SELECT symbol, MIN(close), MAX(close) FROM daily "
                f"WHERE symbol IN ({placeholders}) AND date BETWEEN ? AND ? "
                f"GROUP BY symbol",
                syms + [start_d, end_d]
            ).fetchall()
            if not rows:
                results[name] = {"label": label, "loss_est": None, "note": "no data for period"}
                continue
            total_loss = 0.0
            for sym, min_c, max_c in rows:
                if max_c and max_c > 0:
                    pct = (min_c - max_c) / max_c
                    position_val = weights.get(sym, 0) if isinstance(weights, dict) else 0
                    total_loss += position_val * abs(pct) if pct < 0 else 0
            results[name] = {"label": label, "loss_est": round(total_loss, 2)}
        conn.close()
        return results
    except Exception as e:
        raise  # 错误不吞
        _log.warning(f"stress_test: {e}")
        return {"error": str(e)}


def correlation_breakdown(weights, individual_stds):
    """Worst case: all correlations go to +1 (diversification fails).

    Worst loss = sum(|w_i| * sigma_i) — all positions move together.
    """
    if weights.empty or individual_stds.empty:
        return None
    common = weights.index.intersection(individual_stds.index)
    if len(common) == 0:
        return None
    w = weights.loc[common]
    sig = individual_stds.loc[common]
    return (w.abs() * sig).sum()


def risk_report(positions, portfolio_value, weights, cov_matrix):
    """Generate a complete risk report for the web dashboard.

    Args:
        positions: list of position dicts
        portfolio_value: total portfolio value (RMB)
        weights: pd.Series index=symbol, value=weight (0-1)
        cov_matrix: pd.DataFrame Ledoit-Wolf covariance

    Returns: dict with VaR, CVaR, stress_tests, correlation_breakdown
    """
    report = {
        "timestamp": pd.Timestamp.now().isoformat(),
        "portfolio_value": round(portfolio_value, 2),
    }

    # VaR
    var_95 = compute_var(portfolio_value, weights, cov_matrix, confidence=0.95)
    var_99 = compute_var(portfolio_value, weights, cov_matrix, confidence=0.99)
    report["var"] = {
        "var_95": round(var_95, 2) if var_95 else None,
        "var_95_pct": round(var_95 / portfolio_value * 100, 2) if var_95 and portfolio_value > 0 else None,
        "var_99": round(var_99, 2) if var_99 else None,
        "var_99_pct": round(var_99 / portfolio_value * 100, 2) if var_99 and portfolio_value > 0 else None,
    }

    # CVaR
    cvar_95 = compute_cvar(portfolio_value, weights, cov_matrix, confidence=0.95)
    report["cvar"] = {
        "cvar_95": round(cvar_95, 2) if cvar_95 else None,
        "cvar_95_pct": round(cvar_95 / portfolio_value * 100, 2) if cvar_95 and portfolio_value > 0 else None,
    }

    # Stress tests
    report["stress_tests"] = stress_test(positions, weights)

    # Correlation breakdown
    if cov_matrix is not None and not cov_matrix.empty:
        iv = pd.Series({s: np.sqrt(abs(cov_matrix.loc[s, s]))
                        for s in weights.index if s in cov_matrix.index})
        cb = correlation_breakdown(weights, iv)
        report["correlation_breakdown"] = round(cb, 2) if cb else None

    return report


def update_daily_risk(engine, strategy="quant"):
    """Called from scheduler.attribution: compute daily risk report.

    Returns risk_report dict (also posted to state broker).
    """
    try:
        from risk.covariance import covariance_matrix
        from data.store import DataStore

        positions = engine.get_positions(strategy)
        if not positions:
            return {"available": False, "message": "No positions"}

        total_wealth = engine.get_capital(strategy)
        weights_dict = {}
        for p in positions:
            val = p.get("price", 0) * p.get("shares", 0)
            weights_dict[p["symbol"]] = val / total_wealth if total_wealth > 0 else 0
        weights = pd.Series(weights_dict)

        # Compute covariance from recent market data
        store = DataStore()
        syms = list(weights_dict.keys())
        recent_data = store.get_daily(syms, start="2026-01-01")
        if recent_data is not None and not recent_data.empty:
            log_ret = np.log(recent_data["close"]).diff().dropna(how="all")
            cov = covariance_matrix(log_ret, method="ledoit_wolf")
        else:
            cov = None

        store.close()
        return risk_report(positions, total_wealth, weights, cov)
    except Exception as e:
        raise  # 错误不吞
        _log.warning(f"update_daily_risk: {e}")
        return {"available": False, "error": str(e)}
