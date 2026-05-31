"""模型训练 — 单模型，date-restricted 因子加载，控制内存峰值"""
import numpy as np
import pandas as pd
from data.repository import FactorRepo
from strategy.ensemble import EnsembleModel
from utils.logger import get_logger

logger = get_logger("pipeline.trainer")


def train_model(factors_repo: FactorRepo, all_stocks: list, passed: list,
                train_dates_set: set, y_data_full: pd.Series) -> EnsembleModel:
    """加载 train_window 内的全量因子，训练单个集成模型。

    内存控制：只加载包含在 train_dates_set 中的日期（~504 天而非 1422 天），
    全量 3665 只股票，原始 ~930MB，LightGBM binning 后 ~230MB。
    """
    from config.loader import get as cfg
    train_window = cfg("strategy.train_window", 504)
    train_dates = sorted(train_dates_set)
    if len(train_dates) > train_window:
        train_dates = train_dates[-train_window:]
    start = train_dates[0].strftime("%Y-%m-%d") if hasattr(train_dates[0], "strftime") else str(train_dates[0])[:10]
    end = train_dates[-1].strftime("%Y-%m-%d") if hasattr(train_dates[-1], "strftime") else str(train_dates[-1])[:10]

    logger.info(f"loading factors: {len(all_stocks)} stocks, {start} ~ {end}")
    train_factors = factors_repo.load_batch(all_stocks, start_date=start, end_date=end)
    if train_factors.empty:
        logger.warning("train: no factors loaded")
        return None

    Xf = train_factors[passed]
    common = Xf.index.intersection(y_data_full.index)
    train_mask = [d in train_dates_set for d, _ in common]

    X_tr = Xf.loc[common].values[train_mask]
    y_tr = y_data_full.loc[common].values[train_mask]
    tr_mask = ~(np.isnan(X_tr).any(axis=1) | np.isnan(y_tr))
    X_tr, y_tr = X_tr[tr_mask], y_tr[tr_mask]

    if len(X_tr) < 100:
        logger.warning(f"train: insufficient samples ({len(X_tr)})")
        return None

    model = EnsembleModel()
    model.fit(X_tr, y_tr, passed)
    logger.info(f"trained: {model.model_info}, {len(passed)} factors, {len(X_tr)} samples "
                f"({start}~{end})")
    return model
