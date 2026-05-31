"""IC 因子筛选 + 训练/测试日期划分 — 分块累加统计量，精确等价全量"""
import pandas as pd
from factor.screening import screen_factors_chunked
from data.repository import FactorRepo
from config.loader import get as cfg
from utils.logger import get_logger

logger = get_logger("pipeline.screener")


def screen_and_split(factors_repo: FactorRepo, close_df: pd.DataFrame,
                     all_stocks: list, chunk_size: int = 500) -> dict:
    """分块 IC 筛选 + 训练/测试日期划分。

    用充分统计量跨 chunk 累加（Σx, Σy, Σxy, Σx², Σy², n），最终 IC 与全量计算完全一致，
    不做任何近似。

    训练目标可通过 config 配置:
      - strategy.target: return_1d (默认, 极短期) | return_5d | return_20d
    """
    target = cfg("strategy.target", "return_1d")
    target_map = {"return_1d": 1, "return_5d": 5, "return_20d": 20}
    target_days = target_map.get(target, 1)

    future_ret = close_df.pct_change(target_days).shift(-target_days)
    y_stacked = future_ret.stack(future_stack=True)

    all_dates = sorted(set(close_df.index))
    train_split = cfg("strategy.train_split", 0.7)
    split_idx = int(len(all_dates) * train_split)
    # 排除训练集最后target_days天：其前向收益进入测试期，避免前视偏差
    split_idx_clean = max(0, split_idx - target_days)
    train_dates_set = set(all_dates[:split_idx_clean])
    test_dates_set = set(all_dates[split_idx:])

    # IC阈值从config读取 (激进: min_abs_ic=0.03, min_ic_ir=0.15)
    min_abs_ic = cfg("screening.min_abs_ic", 0.03)
    min_ic_ir = cfg("screening.min_ic_ir", 0.15)
    screening = screen_factors_chunked(
        factors_repo, all_stocks, y_stacked, train_dates_set, chunk_size,
        min_abs_ic=min_abs_ic, min_ic_ir=min_ic_ir
    )
    passed = screening["passed"]

    return {
        "passed": passed,
        "train_dates_set": train_dates_set,
        "test_dates_set": test_dates_set,
        "all_dates": all_dates,
        "split_idx": split_idx,
        "screening": screening,
        "y_data_full": y_stacked,
    }
