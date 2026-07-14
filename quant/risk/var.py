"""Gap 6: Risk management — VaR/CVaR + stress testing + correlation breakdown.

Parametric VaR using existing Ledoit-Wolf covariance matrix.
Historical scenario replay using benchmark_daily stress periods.
Correlation stress test for diversification failure.
"""

import numpy as np
import pandas as pd
import os, sqlite3, json
from scipy.stats import norm
from quant.utils.logger import get_logger

_log = get_logger("risk.var")

_MARKET_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")

# Stress scenarios (CSI 300 worst drawdown periods)
STRESS_SCENARIOS = {
    "2015_gu_zai": ("2015-06-12", "2015-07-08", "2015 股灾 (-32%)"),
    "2018_trade_war": ("2018-01-26", "2018-10-18", "2018 贸易战 (-29%)"),
    "2020_covid": ("2020-01-20", "2020-02-03", "2020 COVID (-12%)"),
    "2024_liquidity": ("2024-01-29", "2024-02-05", "2024 流动性危机 (-8%)"),
}



def historical_var(returns: "pd.Series", confidence: float = 0.95) -> float:
    """Historical VaR: actual percentile of realized returns.

    Unlike parametric VaR (assumes normality), this uses empirical distribution.
    VaR_95 = -percentile(returns, 5%) — the loss exceeded only 5% of the time.

    Args:
        returns: daily PnL returns (positive = gain)
        confidence: 0.95 = VaR_95, 0.99 = VaR_99

    Returns: absolute VaR value (positive number = loss)
    """
    if len(returns) < 20:
        return 0.0
    var = -returns.quantile(1 - confidence)
    return float(abs(var))


def historical_cvar(returns: "pd.Series", confidence: float = 0.95) -> float:
    """Historical CVaR: expected loss beyond VaR threshold."""
    if len(returns) < 20:
        return 0.0
    var = -returns.quantile(1 - confidence)
    tail = returns[returns <= -var]
    if len(tail) == 0:
        return float(abs(var))
    return float(abs(tail.mean()))


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




def marginal_var(weights, cov_matrix, confidence=0.95):
    import numpy as np
    from scipy.stats import norm
    w = weights.values if hasattr(weights,'values') else np.array(list(weights.values()))
    S = cov_matrix.values if hasattr(cov_matrix,'values') else np.array(cov_matrix)
    n = min(len(w), S.shape[0])
    w, S = w[:n], S[:n,:n]
    pv = w.T @ S @ w
    if pv <= 0:
        import pandas as pd
        return pd.Series(0.0, index=weights.index[:n])
    z = norm.ppf(confidence)
    mvar = z * (S @ w) / np.sqrt(pv)
    import pandas as pd
    return pd.Series(mvar, index=weights.index[:n])


def component_var(weights, cov_matrix, confidence=0.95):
    mvar = marginal_var(weights, cov_matrix, confidence)
    w = weights.loc[mvar.index]
    return mvar * w


def stress_test(positions, weights):
    """Historical scenario replay: what if a historical crash happened today?

    Returns: dict[scenario_name] = estimated loss (RMB)
    """
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
    from quant.risk.covariance import covariance_matrix
    from quant.data.store import DataStore

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
    rpt = risk_report(positions, total_wealth, weights, cov)

    # Persist to daily_risk table for backtest audit trail
    _db = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")
    _conn = sqlite3.connect(_db)
    _conn.execute("""CREATE TABLE IF NOT EXISTS daily_risk (
        date TEXT PRIMARY KEY,
        var_95 REAL, var_95_pct REAL, cvar_95 REAL, cvar_95_pct REAL,
        portfolio_value REAL, n_positions INTEGER
    )""")
    _conn.execute(
        "INSERT OR REPLACE INTO daily_risk(date, var_95, var_95_pct, cvar_95, cvar_95_pct, portfolio_value, n_positions) "
        "VALUES (date('now','localtime'), ?, ?, ?, ?, ?, ?)",
        (rpt.get("var", {}).get("var_95"), rpt.get("var", {}).get("var_95_pct"),
         rpt.get("cvar", {}).get("cvar_95"), rpt.get("cvar", {}).get("cvar_95_pct"),
         portfolio_value, len(positions))
    )
    _conn.commit()
    _conn.close()
    return rpt
