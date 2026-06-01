"""构建推荐结果 + sparkline + 行业分布"""
from collections import Counter

import numpy as np
import pandas as pd
from data.store import DataStore
from data.repository import StockRepo, PriceRepo
from engine.backtest_runner import affordable_filter
from utils.logger import get_logger

logger = get_logger("pipeline.builder")


def build_result(pred_series: pd.Series, store: DataStore,
                 close_df: pd.DataFrame, screening: dict,
                 model, all_stocks: list, bt_result: dict,
                 top_n: int = None) -> dict:
    """构建最终结果 dict：推荐列表 + 指标 + 因子 + 行业分布"""
    from config.loader import get as cfg
    if top_n is None:
        top_n = cfg("backtest.max_positions", 3)
    initial_capital = cfg("backtest.initial_capital", 5_000)
    stocks_repo = StockRepo(store)

    # 可买性过滤 (与 backtest_runner 共用同一函数)
    affordable = affordable_filter(pred_series.index.tolist(), close_df, initial_capital)
    if affordable:
        affordable_pred = pred_series.loc[pred_series.index.isin(affordable)]
        top = affordable_pred.head(top_n)
    else:
        top = pred_series.head(top_n)  # fallback
    rec_symbols = list(top.index)
    names = stocks_repo.get_names(rec_symbols)

    # 价格数据补充
    missing_price = [s for s in rec_symbols if s not in close_df.columns]
    extra_close = pd.DataFrame()
    if missing_price:
        extra_raw = store.get_daily(missing_price)
        if not extra_raw.empty:
            extra_close = extra_raw["close"].sort_index()

    returns_all = close_df.pct_change()
    recommendations = []
    for sym, score in top.items():
        score = 0 if np.isnan(score) else float(score)
        if sym in close_df.columns:
            price_series = close_df[sym]
            perf = returns_all[sym]
        elif sym in extra_close.columns:
            price_series = extra_close[sym]
            perf = price_series.pct_change()
        else:
            price_series = pd.Series()
            perf = pd.Series()

        # 安全计算 5 日涨跌幅 (复利, 防 NaN)
        if len(perf) >= 5:
            change_raw = (1 + perf.iloc[-5:]).prod() - 1
            change_5d = round(float(change_raw * 100), 2) if not np.isnan(change_raw) else 0
        else:
            change_5d = 0

        recent = price_series.iloc[-60:].dropna() if len(price_series) >= 60 else price_series.dropna()
        spark = [round(float(x), 2) for x in recent.values] if len(recent) > 0 else []

        recommendations.append({
            "symbol": sym,
            "name": names.get(sym, ""),
            "score": round(float(score), 6),
            "last_price": round(float(price_series.iloc[-1]), 2) if len(price_series) > 0 else 0,
            "change_5d": change_5d,
            "volatility": round(float(perf.std() * 100), 2) if len(perf) > 0 else 0,
            "sparkline": spark,
        })

    # 行业分布
    sector_df = stocks_repo.get_industry_mv(rec_symbols)
    sector_rows = []
    if not sector_df.empty:
        for ind, cnt in Counter(sector_df["industry"].dropna()).items():
            if ind:
                sector_rows.append((ind, cnt))

    result = {
        "recommendations": recommendations,
        "model_info": model.model_info,
        "top_factors": model.feature_importance().head(10).to_dict(),
        "ic_report": screening["ic_report"][:20],
        "n_all_factors": screening["n_total"],
        "n_passed": screening["n_passed"],
        "metrics": {k: round(float(v), 4) for k, v in bt_result["metrics"].items()
                    if isinstance(v, (int, float, np.floating))},
        "data_range": f"{close_df.index[0].date()} ~ {close_df.index[-1].date()}",
        "n_stocks": len(all_stocks),
        "n_predict": len(pred_series),
        "n_days": len(close_df),
        "sector_dist": [{"name": r[0], "value": r[1]} for r in sector_rows],
    }
    if "benchmark" in bt_result:
        result["benchmark"] = bt_result["benchmark"]
    return result
