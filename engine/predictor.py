"""分批预测全量股票"""
import pandas as pd
from data.repository import FactorRepo
from utils.logger import get_logger

logger = get_logger("pipeline.predictor")


def predict_all(factors_repo: FactorRepo, all_stocks: list,
                model, passed: list, batch_size: int = 500) -> pd.Series:
    """分批预测全量股票，返回按得分降序的 Series"""
    if model is None:
        logger.warning("predict_all: model is None, cannot predict")
        return pd.Series()

    # 统一确定最新日期，避免批次间日期不一致且只加载最新1天因子
    global_latest = factors_repo.max_date()
    if global_latest is None:
        logger.warning("predict_all: no factor data available")
        return pd.Series()

    all_predictions = {}

    for i in range(0, len(all_stocks), batch_size):
        chunk = all_stocks[i:i + batch_size]
        batch_factors = factors_repo.load_batch(chunk, start_date=global_latest, end_date=global_latest)
        if batch_factors.empty:
            continue
        try:
            latest_date = global_latest
            latest = batch_factors.xs(pd.to_datetime(latest_date), level=0)
            valid_cols = [c for c in passed if c in latest.columns]
            if not valid_cols:
                continue
            preds = model.predict(latest[valid_cols].values)
            for j, sym in enumerate(latest.index):
                all_predictions[sym] = float(preds[j])
        except Exception:
            logger.warning(f"predict failed for batch {i}-{min(i+batch_size, len(all_stocks))}")
            continue
        if i % 1000 == 0:
            logger.info(f"predict: {min(i+batch_size, len(all_stocks))}/{len(all_stocks)}")

    result = pd.Series(all_predictions).sort_values(ascending=False)
    logger.info(f"predicted: {len(result)} stocks")
    return result
