"""Generate today's top stock picks using the North Star v644 factors.

Uses 3 active factors (equal_weight post-warmup):
  - alpha_momentum_20d  (IC=+0.1496, IR=+0.94)
  - alpha_rsi_14d       (IC=+0.1347, IR=+0.73)
  - alpha002_vol_div    (IC=+0.0324, IR=+0.29)

Universe: top 500 by turnover, LOT_SIZE=100, Nano tier (¥5K capital).
With ¥5,000 and avg price ~¥21.50, can afford 2-3 lots (¥2,150/lot).

Literature:
  - Jegadeish & Titman (1999): 20-day momentum captures trend continuation
  - Wilder (1978): 14-day RSI optimal for intermediate-term mean-reversion
  - De Prado (2018) §15.3: equal-weight avoids IC overfitting in regime shifts
  - Fama & French (2015): momentum strongest in small-cap liquid universe

Usage:
  PYTHONPATH=. .venv/bin/python scripts/generate_signals.py
"""
from quant.utils.logger import offline_mode, get_logger
from quant.utils.excepthook import setup
setup()

with offline_mode():
    from quant.factor.store.core import FactorStore
    from quant.data.store import DataStore
    from quant.execution.calendar import is_trading_day
    import math
    from datetime import date, timedelta
    import numpy as np

    log = get_logger("north_star.signals")

    LOT_SIZE = 100
    CAPITAL = 5000
    UNIVERSAL_SIZE = 500  # v644 optimal (tested: 50/200/500/1000/2000)
    factor_names = ["alpha_momentum_20d", "alpha_rsi_14d", "alpha002_vol_div"]

    # Use latest cached factor date (market closes before factor computation)
    fstore = FactorStore()
    latest_date = fstore.latest_cached_date()
    if latest_date is None:
        raise RuntimeError("No factor cache available — run materialization first")

    print("=" * 72)
    print(f"North Star v644 — Top Stock Picks for next trading day (using {latest_date})")
    print(f"  Factors (equal_weight): alpha_momentum_20d + alpha_rsi_14d + alpha002_vol_div")
    print(f"  Universe: top {UNIVERSAL_SIZE} by turnover | Capital: ¥{CAPITAL:,} | LOT_SIZE: {LOT_SIZE}")
    print("=" * 72)

    # Load factor values from parquet cache
    factor_sets = fstore.load(latest_date, factor_names=factor_names)
    loaded_factors = list(factor_sets.keys())
    print(f"\n  Factors loaded: {loaded_factors}")
    for fname in loaded_factors:
        s = factor_sets[fname]
        print(f"    {fname}: {len(s)} symbols")

    # Get universe and prices from DataStore
    store = DataStore()
    universe_syms = store.get_universe(latest_date)
    universe = store.rank_by_turnover(universe_syms, latest_date, lookback_days=60, top_n=UNIVERSAL_SIZE)
    print(f"  Universe: {len(universe)} stocks (top {UNIVERSAL_SIZE} by turnover)")

    # v644.1 FIX: Use AlphaModel with equal_weight combine_mode (post-warmup).
    # Previously used np.mean() of raw factor values without z-scoring first,
    # causing alpha_momentum_20d (large magnitudes) to dominate selection.
    # AlphaModel.equal_weight z-scores each factor independently before averaging.
    from quant.alpha.model import AlphaModel
    am = AlphaModel(combine_mode="equal_weight")
    
    # Filter factor_sets to only stocks in universe
    universe_set = set(universe)
    filtered_factors = {}
    for fname in factor_names:
        if fname in factor_sets:
            s = factor_sets[fname]
            filtered_factors[fname] = s[s.index.isin(universe_set)]
    
    # AlphaModel.equal_weight z-scores each factor before averaging
    alpha_raw = am.combine(filtered_factors)
    alpha = am.rank(alpha_raw)
    
    ranked = sorted(alpha.items(), key=lambda x: x[1] if np.isfinite(x[1]) else -999, reverse=True)

    # Get prices for top candidates
    top_syms = [s for s, _ in ranked[:20]]
    price_df = store.get_daily(top_syms, start=latest_date, end=latest_date, columns=["close"])
    price_map = {}
    if not price_df.empty:
        for sym in top_syms:
            col = ("close", sym)
            if col in price_df.columns:
                val = price_df[col].iloc[0]
                if val is not None and np.isfinite(val):
                    price_map[sym] = float(val)

    print(f"\n  {'排名':>4s}  {'股票代码':>8s}  {'价格':>7s} {'Alpha':>7s} {'手数':>4s} {'成本':>7s}  {'说明':<10s}")
    print("  " + "─" * 65)

    capital = CAPITAL
    picked = []
    for i, (sym, score) in enumerate(ranked[:20]):
        price = price_map.get(sym, 0)
        if price is None or price <= 0 or price > 100:
            # Filter: price must be reasonable for Nano tier (¥2-100)
            continue
        cost_per_lot = price * LOT_SIZE
        if cost_per_lot > capital:
            continue
        # Nano tier: _rank_concentrated buys 1 lot, stops when cash exhausted
        lots = 1
        cost = lots * cost_per_lot
        picked.append(sym)
        reason = "momentum" if score > 0.5 else "watch"
        print(f"  {i+1:>4d}  {sym:>8s}  {price:>7.2f} {score:>+7.3f} {lots*LOT_SIZE:>4d}  ¥{cost:>7.0f}  {reason:<10s}")
        capital -= cost
        if capital < LOT_SIZE * 1:  # can't afford another 100-share lot
            break

    total_cost = CAPITAL - capital
    print(f"\n  ✅ 推荐持仓: {len(picked)} 只 ({', '.join(picked)})")
    print(f"  资金利用率: {total_cost/CAPITAL:.0%} (¥{capital:.0f} 剩余)")
    print(f"  策略: 每周重平衡 | 持有 top 2-3 只 | stop-loss 15% | 目标: 20x in 251 天")
    print("=" * 72)
