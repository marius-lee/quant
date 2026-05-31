"""妖股信号叠加 + 行业市值中性化 + 排名"""
import numpy as np
import pandas as pd
from data.store import DataStore
from factor.demon import DemonSignals
from data.repository import StockRepo
from config.loader import get as cfg
from utils.logger import get_logger

logger = get_logger("pipeline.ranker")


def apply_demon_and_neutralize(pred_series: pd.Series, store: DataStore,
                               demon: DemonSignals, stocks_repo: StockRepo) -> pd.Series:
    """对全量预测得分叠加妖股信号 + 可选行业市值中性化。

    北极星目标模式 (neutralize=False):
      - 妖股信号占80%权重, ML仅作辅助
      - 不做行业市值中性化 (行业集中和小盘暴露是主动alpha来源)
    """
    all_demon_stocks = list(pred_series.index)
    raw_demon = store.get_daily(all_demon_stocks)

    if raw_demon.empty:
        return pred_series

    demon_close = raw_demon["close"].sort_index()
    demon_vol = raw_demon.get("volume", demon_close * 1e7)
    demon_high = raw_demon.get("high", demon_close * 1.01)
    demon_low = raw_demon.get("low", demon_close * 0.99)
    demon_scores = demon.compute(demon_close, demon_vol, demon_high, demon_low)
    latest_demon = demon_scores.iloc[-1].clip(0, 1)

    # ML得分(归一化) + 妖股得分，权重可配置 (默认激进: ML=20%, Demon=80%)
    ml_min, ml_max = pred_series.min(), pred_series.max()
    ml_norm = ((pred_series - ml_min) / (ml_max - ml_min + 1e-10)).fillna(0)
    latest_demon_clean = latest_demon.reindex(pred_series.index).fillna(0).clip(0, 1)
    ml_weight = cfg("ranker.ml_weight", 0.2)
    demon_weight = 1.0 - ml_weight
    combined = ml_norm * ml_weight + latest_demon_clean * demon_weight

    # 行业+市值中性化 (可通过config关闭，默认关闭)
    if cfg("ranker.neutralize", False):
        combined = _neutralize(combined.fillna(0), stocks_repo)
    combined = combined.fillna(0)
    result = combined.sort_values(ascending=False)
    logger.info(f"demon (w={demon_weight:.1f}) + neutralization={'on' if cfg('ranker.neutralize', False) else 'off'} done")
    return result


def _neutralize(scores: pd.Series, stocks_repo: StockRepo) -> pd.Series:
    """行业+市值中性化（不含截距项）"""
    symbols = list(scores.index)
    info = stocks_repo.get_industry_mv(symbols)
    if info.empty or info["total_mv"].sum() == 0:
        return scores

    df = info.set_index("symbol")
    log_mv = np.log(df["total_mv"].clip(lower=1))
    industries = df["industry"].fillna("其他")

    common = [s for s in scores.index if s in df.index]
    y = scores[common].values
    cols = [log_mv.reindex(common).fillna(0).values]
    ind_counts = industries.value_counts()
    for ind in industries.unique():
        if ind and ind != "其他" and ind_counts.get(ind, 0) >= 2:
            cols.append((industries.reindex(common) == ind).astype(float).values)
    X = np.column_stack(cols)

    try:
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
        resid = y - X @ beta
        return pd.Series(resid, index=common).sort_values(ascending=False)
    except Exception:
        logger.warning("neutralization failed, returning raw scores")
        return scores
