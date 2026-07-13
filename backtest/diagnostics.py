"""回测诊断与因子归因模块。

四层架构的 Layer 1 (因子评估) 和 Layer 4 (业绩归因):
  1. 回测前置 rolling IC — 用起点之前的数据评估因子有效性
  2. 盘中因子贡献跟踪 — 记录每日每个因子对选股的贡献
  3. 盘后诊断 — 分析回测结果，输出因子权重调整建议
"""

from utils.logger import get_logger
from config.constants import _require_cfg
import pandas as pd
import numpy as np

_log = get_logger("backtest.diagnostics")


def compute_pre_backtest_ic(factor_names: list, date: str, symbols: list,
                            lookback: int, store=None) -> dict:
    """回测前置 IC — 委托 factor/ic.py 统一计算。

    消除 look-ahead bias：只用 date 之前的数据。
    返回: {factor_name: {"ic_mean": float, "ic_ir": float, "weight": float}}
    """
    from factor.ic import compute_ic as _unified_ic
    return _unified_ic(factor_names, date, symbols, lookback,
                       store=store, status_filter="backtesting")


class FactorTracker:
    """逐日跟踪因子对持仓的贡献。"""

    def __init__(self):
        self.records = []  # [{date, factor_name, symbol, score, weight, pnl_contribution}]

    def record_day(self, date: str, factor_values: dict, alpha_scores: pd.Series,
                   positions: list, returns: pd.Series):
        """记录一天的因子贡献。

        factor_values: {name: Series(symbol→score)}
        alpha_scores: Series(symbol→combined_alpha)
        positions: [{symbol, weight, ...}]
        returns: Series(symbol→next_day_return)
        """
        if not positions or alpha_scores.empty:
            return

        # 对每个持仓，计算每个因子的边际贡献
        symbols_held = [p["symbol"] for p in positions if p.get("symbol") in returns.index]
        if not symbols_held:
            return

        total_alpha = alpha_scores.loc[symbols_held].abs().sum()
        if total_alpha == 0:
            return

        for sym in symbols_held:
            sym_ret = returns.get(sym, 0)
            for fname, fseries in factor_values.items():
                if isinstance(fseries, pd.Series) and sym in fseries.index:
                    fscore = fseries.get(sym, 0)
                    fcontribution = abs(fscore) / max(total_alpha, 1e-10)
                    self.records.append({
                        "date": date,
                        "factor_name": fname,
                        "symbol": sym,
                        "score": round(float(fscore), 4),
                        "weight": round(fcontribution, 4),
                        "pnl_contribution": round(float(sym_ret * fcontribution * 100), 6),
                    })


def diagnose(ic_map: dict, tracker: FactorTracker, metrics: dict) -> dict:
    """回测后诊断 — 输出因子权重调整建议。

    Returns:
        {
            "factor_report": {name: {ic_mean, ic_ir, pnl_contrib, recommendation}},
            "adjustments": [(action, param, old_val, new_val)],
            "summary": str,
        }
    """
    report = {}

    # 1. 从 IC map 拿 IC 统计
    for name, info in ic_map.items():
        report[name] = {
            "ic_mean": info["ic_mean"],
            "ic_ir": info["ic_ir"],
            "pre_bt_weight": info["weight"],
            "pnl_contrib": 0.0,
            "n_trades": 0,
            "recommendation": "keep",
        }

    # 2. 聚合因子 PnL 贡献
    if tracker.records:
        df = pd.DataFrame(tracker.records)
        pnl_by_factor = df.groupby("factor_name")["pnl_contribution"].sum()
        trades_by_factor = df.groupby("factor_name").size()
        for name in report:
            report[name]["pnl_contrib"] = round(float(pnl_by_factor.get(name, 0)), 4)
            report[name]["n_trades"] = int(trades_by_factor.get(name, 0))

    # 3. 生成调整建议
    adjustments = []
    for name, info in report.items():
        ic_ir = info["ic_ir"]
        pnl = info["pnl_contrib"]
        n = info["n_trades"]

        diag_min_icir = _require_cfg("factor.evaluation.diagnostics_min_icir")
        diag_pnl_threshold = _require_cfg("factor.evaluation.diagnostics_pnl_threshold")
        diag_review_threshold = _require_cfg("factor.evaluation.diagnostics_review_threshold")

        if ic_ir < diag_min_icir and n < 5:
            info["recommendation"] = "drop"
            adjustments.append(("drop_factor", name, "active", f"drop (ICIR<{diag_min_icir}, low trades)"))
        elif pnl < diag_review_threshold and ic_ir > 0:
            # 因子有 IC 但实际 PnL 为负 → 可能是交易成本或组合约束问题
            info["recommendation"] = "review"
            adjustments.append(("adjust_weight", name, info["pre_bt_weight"], max(0.01, info["pre_bt_weight"] * 0.5)))
        elif pnl > diag_pnl_threshold:
            info["recommendation"] = "boost"
            new_w = min(1.0, info["pre_bt_weight"] * 1.5)
            adjustments.append(("adjust_weight", name, info["pre_bt_weight"], new_w))
        elif n == 0:
            info["recommendation"] = "unused"
            adjustments.append(("drop_factor", name, "active", "unused (never selected)"))

    # 4. Summary
    n_active = sum(1 for v in report.values() if v["recommendation"] in ("keep", "boost"))
    n_drop = sum(1 for v in report.values() if v["recommendation"] == "drop")
    n_review = sum(1 for v in report.values() if v["recommendation"] == "review")
    n_unused = sum(1 for v in report.values() if v["recommendation"] == "unused")

    summary = (
        f"Diagnosis: {len(report)} factors → "
        f"keep/boost={n_active}, drop={n_drop}, review={n_review}, unused={n_unused}. "
        f"Sharpe={metrics.get('sharpe', 0)}, CAGR={metrics.get('cagr_pct', 0)}%"
    )

    return {
        "factor_report": report,
        "adjustments": adjustments,
        "summary": summary,
    }


def apply_diagnosis(ic_map: dict, diagnosis: dict) -> dict:
    """根据诊断结果调整 IC map，生成下一轮回测的权重。

    返回新的 ic_map，可直接传入 AlphaModel.combine。
    """
    new_map = {}
    report = diagnosis.get("factor_report", {})

    for name, info in report.items():
        if info["recommendation"] == "drop":
            continue  # 移除该因子
        if name in ic_map:
            entry = dict(ic_map[name])
            if info["recommendation"] == "boost":
                entry["weight"] = min(1.0, entry.get("weight", 0.1) * 1.5)
            elif info["recommendation"] == "review":
                entry["weight"] = max(0.01, entry.get("weight", 0.1) * 0.5)
            new_map[name] = entry

    return new_map
