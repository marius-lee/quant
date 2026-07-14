"""Gap 2: Hyperparameter optimization — Optuna search over config space.

Requires: pip install optuna
Usage:
    PYTHONPATH=. python3 optimizer/hyperopt.py

Runs 200 Optuna trials, each running a full backtest (from backtest.loop),
and saves the best parameters to config/best_params.json.
"""

import os, sys, json, time
_root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _root)

import numpy as np
from quant.utils.logger import get_logger

_log = get_logger("optimizer.hyperopt")


def objective(trial):
    """Optuna objective: run backtest with trial params, return net Sharpe."""
    try:
        import optuna
    except ImportError:
        _log.error("optuna not installed — pip install optuna")
        return 0.0

    from quant.backtest.loop import run_backtest

    # ── Hyperparameters to optimize (11 params) ──
    params = {
        "n_symbols": trial.suggest_int("n_symbols", 200, 800, step=100),
        "lookback_days": trial.suggest_int("lookback_days", 60, 365, step=60),
        "top_fraction": trial.suggest_float("top_fraction", 0.1, 0.5, step=0.05),
        "max_positions": trial.suggest_int("max_positions", 5, 30, step=5),
        "max_single_position": trial.suggest_float("max_single_position", 0.03, 0.15, step=0.02),
        "covariance_window": trial.suggest_int("covariance_window", 30, 120, step=30),
        "atr_sl": trial.suggest_float("atr_sl", 1.5, 3.0, step=0.25),
        "atr_tp1": trial.suggest_float("atr_tp1", 1.5, 3.0, step=0.25),
        "atr_tp2": trial.suggest_float("atr_tp2", 2.5, 4.0, step=0.25),
        "combine_mode": trial.suggest_categorical("combine_mode", ["sleeve", "ic_weighted"]),
        "rebalance_freq": trial.suggest_categorical("rebalance_freq", ["daily", "weekly"]),
    }

    # ── Apply params to config (in-process override) ──
    # We use environment variables to override config during backtest
    os.environ["OPTUNA_N_SYMBOLS"] = str(params["n_symbols"])
    os.environ["OPTUNA_LOOKBACK"] = str(params["lookback_days"])
    os.environ["OPTUNA_TOP_FRAC"] = str(params["top_fraction"])
    os.environ["OPTUNA_MAX_POS"] = str(params["max_positions"])
    os.environ["OPTUNA_MAX_SINGLE"] = str(params["max_single_position"])
    os.environ["OPTUNA_COV_WIN"] = str(params["covariance_window"])
    os.environ["OPTUNA_ATR_SL"] = str(params["atr_sl"])
    os.environ["OPTUNA_ATR_TP1"] = str(params["atr_tp1"])
    os.environ["OPTUNA_ATR_TP2"] = str(params["atr_tp2"])
    os.environ["OPTUNA_COMBINE_MODE"] = params["combine_mode"]
    os.environ["OPTUNA_REBALANCE"] = params["rebalance_freq"]

    # ── Run backtest (shorter period for speed) ──
    result = run_backtest(
        start_date="2023-01-01",
        end_date="2024-12-31",
        capital=5000,
        strategy=f"optuna_{trial.number}",
    )

    if "error" in result:
        return 0.0

    metrics = result["metrics"]
    sharpe = metrics["sharpe"]
    mdd = abs(metrics["max_drawdown_pct"])

    # ── Penalty for excessive drawdown ──
    if mdd > 30:
        sharpe *= 0.5
    elif mdd > 20:
        sharpe *= 0.8

    # ── Store trial metadata ──
    trial.set_user_attr("cagr", metrics["cagr_pct"])
    trial.set_user_attr("mdd", metrics["max_drawdown_pct"])
    trial.set_user_attr("final_equity", metrics["final_equity"])
    trial.set_user_attr("errors", result["errors"])

    return sharpe


def run_optimization(n_trials=200, study_name="quant_hyperopt"):
    """Run Optuna optimization and save best params."""
    import optuna

    _log.info(f"Optuna hyperparameter optimization: {n_trials} trials")

    study = optuna.create_study(
        study_name=study_name,
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(
            n_startup_trials=20,
            n_warmup_steps=10,
        ),
        storage=f"sqlite:///data/optuna_{study_name}.db",
        load_if_exists=True,
    )

    t0 = time.time()
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)
    elapsed = time.time() - t0

    _log.info(f"Optimization done in {elapsed/3600:.1f}h")
    _log.info(f"Best trial #{study.best_trial.number}: Sharpe={study.best_value:.3f}")

    # Save best params
    best_path = os.path.join(_root, "config", "best_params.json")
    best = {
        "trial_number": study.best_trial.number,
        "sharpe": study.best_value,
        "params": study.best_params,
        "trial_attrs": {
            "cagr": study.best_trial.user_attrs.get("cagr"),
            "mdd": study.best_trial.user_attrs.get("mdd"),
            "final_equity": study.best_trial.user_attrs.get("final_equity"),
        },
        "elapsed_hours": round(elapsed / 3600, 1),
    }

    with open(best_path, "w") as f:
        json.dump(best, f, indent=2, ensure_ascii=False)
    _log.info(f"Best params saved to {best_path}")

    # Clean up env vars
    for k in list(os.environ.keys()):
        if k.startswith("OPTUNA_"):
            del os.environ[k]

    return best


if __name__ == "__main__":
    run_optimization(n_trials=200)
