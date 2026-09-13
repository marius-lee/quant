"""TradeRepo 列名常量."""

from __future__ import annotations

# ── sim_trades ──
ST_DATE         = "date"
ST_SYMBOL       = "symbol"
ST_SIDE         = "side"
ST_PRICE        = "price"
ST_SHARES       = "shares"
ST_PNL          = "pnl"
ST_PNL_PCT      = "pnl_pct"
ST_CAPITAL_AFTER = "capital_after"
ST_STRATEGY     = "strategy"
ST_MODE         = "mode"
ST_BOARD_COUNT  = "board_count"
ST_COST         = "cost"
ST_CREATED_AT   = "created_at"

# strategy_config
SC_STRATEGY       = "strategy"
SC_MODE           = "mode"
SC_INITIAL_CAPITAL = "initial_capital"
SC_INITIALIZED    = "initialized"
SC_UPDATED_AT     = "updated_at"

# daily_signals
DS_DATE         = "date"
DS_STRATEGY     = "strategy"
DS_SIGNALS_JSON = "signals_json"
DS_CAPITAL      = "capital"
DS_GENERATED_AT = "generated_at"
DS_MODE         = "mode"
DS_EXEC_NOTES   = "exec_notes"

# pending_orders
PO_ID             = "id"
PO_STRATEGY      = "strategy"
PO_SYMBOL        = "symbol"
PO_SIDE          = "side"
PO_TARGET_SHARES = "target_shares"
PO_LIMIT_PRICE   = "limit_price"
PO_REFERENCE_PRICE = "reference_price"
PO_STATUS        = "status"
PO_PLACED_AT     = "placed_at"
PO_FILLED_AT     = "filled_at"
PO_FILLED_SHARES = "filled_shares"
PO_FILLED_PRICE  = "filled_price"
PO_CHASE_COUNT   = "chase_count"
PO_CANCEL_REASON = "cancel_reason"
PO_DAY           = "day"
PO_MODE          = "mode"

# daily_equity
DE_DATE           = "date"
DE_CASH           = "cash"
DE_POSITION_VALUE = "position_value"
DE_TOTAL_EQUITY   = "total_equity"
DE_DRAWDOWN_PCT   = "drawdown_pct"

# position_meta
PM_SYMBOL     = "symbol"
PM_DAY        = "day"
PM_TP1_HIT    = "tp1_hit"
PM_PEAK_PRICE = "peak_price"

# benchmark_tracking
BT_DATE              = "date"
BT_STRATEGY_EQUITY   = "strategy_equity"
BT_STRATEGY_RETURN   = "strategy_return"
BT_BENCH_RETURN      = "bench_return"
BT_ALPHA             = "alpha"
BT_ROLLING_ALPHA_60D = "rolling_alpha_60d"
BT_ROLLING_IR_60D    = "rolling_ir_60d"
BT_ROLLING_BETA_60D  = "rolling_beta_60d"
BT_UP_CAPTURE_60D    = "up_capture_60d"
BT_DOWN_CAPTURE_60D  = "down_capture_60d"

__all__ = [
    'ST_DATE', 'ST_SYMBOL', 'ST_SIDE', 'ST_PRICE', 'ST_SHARES',
    'ST_PNL', 'ST_PNL_PCT', 'ST_CAPITAL_AFTER', 'ST_STRATEGY', 'ST_MODE',
    'ST_BOARD_COUNT', 'ST_COST', 'ST_CREATED_AT',
    'SC_STRATEGY', 'SC_MODE', 'SC_INITIAL_CAPITAL', 'SC_INITIALIZED', 'SC_UPDATED_AT',
    'DS_DATE', 'DS_STRATEGY', 'DS_SIGNALS_JSON', 'DS_CAPITAL', 'DS_GENERATED_AT', 'DS_MODE', 'DS_EXEC_NOTES',
    'PO_ID', 'PO_STRATEGY', 'PO_SYMBOL', 'PO_SIDE', 'PO_TARGET_SHARES', 'PO_LIMIT_PRICE',
    'PO_REFERENCE_PRICE', 'PO_STATUS', 'PO_PLACED_AT', 'PO_FILLED_AT', 'PO_FILLED_SHARES',
    'PO_FILLED_PRICE', 'PO_CHASE_COUNT', 'PO_CANCEL_REASON', 'PO_DAY', 'PO_MODE',
    'DE_DATE', 'DE_CASH', 'DE_POSITION_VALUE', 'DE_TOTAL_EQUITY', 'DE_DRAWDOWN_PCT',
    'PM_SYMBOL', 'PM_DAY', 'PM_TP1_HIT', 'PM_PEAK_PRICE',
    'BT_DATE', 'BT_STRATEGY_EQUITY', 'BT_STRATEGY_RETURN', 'BT_BENCH_RETURN', 'BT_ALPHA',
    'BT_ROLLING_ALPHA_60D', 'BT_ROLLING_IR_60D', 'BT_ROLLING_BETA_60D',
    'BT_UP_CAPTURE_60D', 'BT_DOWN_CAPTURE_60D',
]