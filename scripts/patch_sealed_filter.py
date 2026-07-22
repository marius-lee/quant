import re

# 1. Add filter_sealed_limit_up to constraints.py
cp = open('/Users/mariusto/project/quant/quant/risk/constraints.py').read()

marker = '\n\ndef apply_all_filters('
assert marker in cp, 'marker not found'

new_func = '''

def filter_sealed_limit_up(candidates, prev_date: str, seal_ratio_threshold: float = 3.0):
    """Exclude stocks sealed at limit-up on previous trading day.

    Sources: ADR-033 limit order design + test-v210 exec feedback loop.
    limit_up_pool table in market.db, synced daily by daily_sync.py.
    seal_ratio = lock_capital / amount; higher = stronger seal.
    """
    import sqlite3
    from quant.config.paths import MARKET_DB
    conn = sqlite3.connect(MARKET_DB)
    try:
        rows = conn.execute(
            "SELECT symbol, lock_capital, amount FROM limit_up_pool WHERE date=?",
            (prev_date,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return candidates.copy()
    sealed_syms = set()
    for sym, lock_cap, amt in rows:
        try:
            lc = float(lock_cap or 0)
            a = float(amt or 0)
            if lc > 0 and a > 0 and (lc / a) > seal_ratio_threshold:
                sealed_syms.add(sym)
        except (ValueError, TypeError):
            pass
    from quant.utils.logger import get_logger
    logger = get_logger("risk.constraints")
    removed = [s for s in sealed_syms if s in candidates.index]
    if removed:
        logger.info(f"limit-up filter: {prev_date} sealed={len(sealed_syms)} removed={len(removed)}")
    return candidates[~candidates.index.isin(sealed_syms)].copy()

'''

cp = cp.replace(marker, new_func + marker)
open('/Users/mariusto/project/quant/quant/risk/constraints.py', 'w').write(cp)
import ast
ast.parse(cp)
print("constraints.py: OK")

# 2. Add call in pipeline.py Step 2.3
pp = open('/Users/mariusto/project/quant/quant/pipeline.py').read()

old_pp = '''    _pre_filtered = apply_all_filters(_pre_df, limits=_risk_limits, stock_names=store.get_stock_names(symbols))
    investable_symbols = _pre_filtered.index.tolist()
    logger.info(f"[2.3] risk pre-filters: {len(symbols)} -> {len(investable_symbols)} investable "
                f"(liquidity>{_risk_limits.min_daily_amount}, price>{_risk_limits.min_price}, no ST)")'''

new_pp = '''    _pre_filtered = apply_all_filters(_pre_df, limits=_risk_limits, stock_names=store.get_stock_names(symbols))
    # ── 涨停封死预过滤 (test-v211): 昨日封成比>阈值的股票今日无法交易 ──
    from quant.risk.constraints import filter_sealed_limit_up
    from quant.execution.calendar import prev_trading_day
    _prev_day = prev_trading_day(date)
    if _prev_day:
        _pre_filtered = filter_sealed_limit_up(_pre_filtered, _prev_day,
                                                seal_ratio_threshold=_require_cfg("universe.sealed_limit_up_ratio"))
    investable_symbols = _pre_filtered.index.tolist()
    logger.info(f"[2.3] risk pre-filters: {len(symbols)} -> {len(investable_symbols)} investable "
                f"(liquidity>{_risk_limits.min_daily_amount}, price>{_risk_limits.min_price}, no ST, limit-up)")'''

assert old_pp in pp, 'pipeline block not found'
pp = pp.replace(old_pp, new_pp)
open('/Users/mariusto/project/quant/quant/pipeline.py', 'w').write(pp)
ast.parse(pp)
print("pipeline.py: OK")

# 3. Add config key to config.yaml
import yaml
with open('/Users/mariusto/project/quant/quant/config/config.yaml') as f:
    cfg = yaml.safe_load(f)
if 'universe' not in cfg:
    cfg['universe'] = {}
cfg['universe']['sealed_limit_up_ratio'] = 3.0
with open('/Users/mariusto/project/quant/quant/config/config.yaml', 'w') as f:
    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
print("config.yaml: OK")
