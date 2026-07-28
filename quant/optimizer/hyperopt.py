"""Gap 2: Hyperparameter optimization — Optuna search over config space.

Requires: pip install optuna
Usage:
    PYTHONPATH=. python3 -m quant.optimizer.hyperopt

Runs N Optuna trials, each running a full backtest (from backtest.loop),
and saves the best parameters to config/best_params.json.

test-v298 (待办 #9): 参数注入机制修复 — 旧版把 11 个参数写进 OPTUNA_*
环境变量, 但全项目无任何代码读这些变量, Optuna 实际在优化常数目标函数。
现改为: config 参数经 loader.override() 直接覆盖 config 单例 (回测内
运行时 _require_cfg 全部生效); universe_size / combine_mode 作为
run_backtest() 显式参数传入。

搜索空间说明 (死维度已修):
  - lookback_days 下限 400: 低于 max_factor_calendar_days(=378) 的值会被
    pipeline `_eff_days = max(lookback, 378)` clamp 成常数, 旧范围 60-365 全无效。
  - max_single_position 已删除: Nano 层 (capital=5000) _rank_concentrated
    不使用该参数, 是死维度。
"""

import os, sys, json, time
_root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _root)

import numpy as np
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

_log = get_logger("optimizer.hyperopt")

# ── 搜索参数 → config key 映射 (test-v298) ──
# 消费点核实 (2026-07-26, 全部运行时读取, override 即时生效):
#   data.lookback_days       → pipeline Step 2 `_eff_days` (每次 generate_signals)
#   alpha.top_fraction       → AlphaModel.__init__ (pipeline 每次新建)
#   risk.max_positions       → PortfolioConstructor.__init__ (每次新建)
#   risk.covariance.window   → covariance.py covariance_matrix() 内
#   risk.atr_mult_*          → stop_loss.py RiskManager.__init__ (每次新建)
#   optimizer.rebalance_freq → loop.py is_rebalance_day (每个交易日)
# universe_size / combine_mode 不在此表 — 走 run_backtest 显式参数。
_PARAM_TO_CONFIG = {
    "lookback_days": "data.lookback_days",
    "top_fraction": "alpha.top_fraction",
    "max_positions": "risk.max_positions",
    "covariance_window": "risk.covariance.window",
    "atr_sl": "risk.atr_mult_stop_loss",
    "atr_tp1": "risk.atr_mult_take_profit_1",
    "atr_tp2": "risk.atr_mult_take_profit_2",
    "rebalance_freq": "optimizer.rebalance_freq",
}


def objective(trial):
    """Optuna objective: run backtest with trial params, return net Sharpe."""
    try:
        import optuna
    except ImportError:
        _log.error("optuna not installed — pip install optuna")
        return 0.0

    from quant.backtest.loop import run_backtest
    from quant.config import loader as _cfg_loader

    # ── Hyperparameters to optimize (10 params) ──
    params = {
        "n_symbols": trial.suggest_int("n_symbols", 200, 800, step=100),
        "lookback_days": trial.suggest_int("lookback_days", 400, 800, step=100),
        "top_fraction": trial.suggest_float("top_fraction", 0.1, 0.5, step=0.05),
        "max_positions": trial.suggest_int("max_positions", 5, 30, step=5),
        "covariance_window": trial.suggest_int("covariance_window", 30, 120, step=30),
        "atr_sl": trial.suggest_float("atr_sl", 1.5, 3.0, step=0.25),
        "atr_tp1": trial.suggest_float("atr_tp1", 1.5, 3.0, step=0.25),
        "atr_tp2": trial.suggest_float("atr_tp2", 2.5, 4.0, step=0.25),
        "combine_mode": trial.suggest_categorical("combine_mode", ["sleeve", "ic_weighted"]),
        "rebalance_freq": trial.suggest_categorical("rebalance_freq", ["daily", "weekly"]),
    }

    # ── Apply params: config 单例覆盖 + run_backtest 显式参数 ──
    overrides = {_PARAM_TO_CONFIG[k]: v for k, v in params.items()
                 if k in _PARAM_TO_CONFIG}

    try:
        with _cfg_loader.override(overrides):
            result = run_backtest(
                start_date="2023-01-01",
                end_date="2024-12-31",
                capital=_require_cfg("backtest.default_capital"),
                strategy=f"optuna_{trial.number}",
                universe_size=params["n_symbols"],
                combine_mode=params["combine_mode"],
            )
    except Exception as e:
        _log.error(f"trial {trial.number} backtest crashed: {e}")
        return 0.0

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
        storage=f"sqlite:///quant/data/optuna_{study_name}.db",
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

    return best


if __name__ == "__main__":
    run_optimization(n_trials=200)
