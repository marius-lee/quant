"""v418 (R4): generate_signals 参数收敛 — preload 依赖移入 BacktestContext.

对应 docs/reports/CODE-REVIEW-2026-08-07.md (26 参数 → 18).
"""
import inspect
from quant.pipeline import generate_signals


def test_generate_signals_param_count_shrunk():
    sig = inspect.signature(generate_signals)
    assert len(sig.parameters) == 18


def test_preload_kwargs_removed_from_signature():
    sig = inspect.signature(generate_signals)
    removed = {
        "preloaded_data", "factor_cache", "turnover_amount_roll", "bm_returns",
        "stock_names", "preloaded_seal_ratios", "prebuilt_engine",
        "prebuilt_cost_model", "prebuilt_constructor", "fund_stocks_df",
        "fund_val_piv", "fund_close_piv", "fund_high_52w", "all_symbols",
    }
    assert removed.isdisjoint(sig.parameters)


def test_business_params_preserved():
    sig = inspect.signature(generate_signals)
    for k in ("date_str", "capital", "strategy", "skip_pull", "store",
              "status_filter", "scope", "suppress_push", "universe_size",
              "db_path", "exclude_symbols", "ic_map", "combine_mode",
              "primitives", "factor_store", "regime_label", "regime_probs", "ctx"):
        assert k in sig.parameters, f"missing {k}"


def test_valid_defaults():
    sig = inspect.signature(generate_signals)
    for k in ("preloaded_data", "factor_cache", "turnover_amount_roll"):
        assert k not in sig.parameters