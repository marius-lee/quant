"""真实基本面因子 — PE/PB/市值/股息/52周位置/换手率

从 stocks 表读取腾讯财经数据，生成截面因子供因子缓存使用。
"""
import numpy as np
import pandas as pd
from data.store import DataStore


def compute(close_df: pd.DataFrame, symbols: list, store: DataStore,
            full_rebuild: bool = False) -> pd.DataFrame:
    """返回 (date,stock) × [real_ep, real_ep_ttm, real_bp, real_div, ...]

    full_rebuild=True: 全量重建模式，跳过真实基本面因子（避免最新PE/PB广播到历史日期导致前视偏差）。
    仅增量更新（full_rebuild=False）时才使用最新市价计算因子。
    """
    if full_rebuild:
        return pd.DataFrame()

    conn = store._connect()
    ph = ",".join("?" for _ in symbols)
    cols = "symbol,pe,pe_ttm,pb,total_mv,div_yield,cfps,high_52w,low_52w,turnover_rate"
    info = pd.read_sql_query(
        f"SELECT {cols} FROM stocks WHERE symbol IN ({ph})", conn, params=symbols
    ).set_index("symbol")
    conn.close()

    if info.empty:
        return pd.DataFrame()

    price = close_df.iloc[-1]
    factors = {}
    for sym in symbols:
        if sym not in info.index:
            continue
        row = info.loc[sym]
        pe_val = row["pe"] or 0
        pb_val = row["pb"] or 0
        total_mv_val = row["total_mv"] or 1
        high52 = row["high_52w"] or 0
        low52 = row["low_52w"] or 0

        factors[("real_ep", sym)] = 1.0 / pe_val if pe_val > 0 else 0
        factors[("real_ep_ttm", sym)] = 1.0 / (row["pe_ttm"] or 0) if (row["pe_ttm"] or 0) > 0 else 0
        factors[("real_bp", sym)] = 1.0 / pb_val if pb_val > 0 else 0
        factors[("real_div", sym)] = (row["div_yield"] or 0) / 100
        cfp = (row["cfps"] or 0) / max(price.get(sym, 0.01), 0.01)
        factors[("real_cf_price", sym)] = max(-1, min(5, cfp))
        factors[("real_log_mv", sym)] = np.log(max(total_mv_val, 1))
        rng = high52 - low52
        factors[("real_52w_pos", sym)] = (
            max(0, min(1, (price.get(sym, 0) - low52) / rng))
            if rng > 0 else 0.5
        )
        factors[("real_turnover", sym)] = (row["turnover_rate"] or 0) / 100

    if not factors:
        return pd.DataFrame()

    result = pd.DataFrame(factors, index=close_df.index)
    result.columns = pd.MultiIndex.from_tuples(result.columns)
    return result.stack(level=1, future_stack=True)
