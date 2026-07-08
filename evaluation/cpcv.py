"""Purged Walk-Forward Cross-Validation — De Prado (2018) Chapter 7.

关键概念:
  - Purging: 删除训练集中与测试集有时间重叠的观测, 防止信息泄露
  - Embargo: 在测试集之后额外跳过 h 天, 防止序列相关污染
  - 在已计算的 IC 时间序列上做 folding, 避免重复计算因子
"""

import numpy as np
import pandas as pd
from utils.logger import get_logger


class PurgedWalkForward:
    """Purged walk-forward cross-validator for factor IC time series.

    Parameters
    ----------
    n_groups : int
        Number of cross-validation groups (default 5, config: factor.evaluation.cpcv_groups)
    embargo_days : int
        Days to embargo after test set (default 1, config: factor.evaluation.embargo_days)

    Usage
    -----
    pvf = PurgedWalkForward(n_groups=5, embargo_days=1)
    for fold_i, (train_dates, test_dates) in enumerate(pvf.split(dates)):
        train_ic = ic_series.loc[train_dates].mean()
        test_ic = ic_series.loc[test_dates].mean()
    """

    def __init__(self, n_groups: int = 5, embargo_days: int = 1):
        if n_groups < 2:
            raise ValueError(f"n_groups must be >= 2, got {n_groups}")
        self.n_groups = n_groups
        self.embargo_days = embargo_days

    def split(self, dates: list) -> list[tuple[list, list]]:
        logger = get_logger("evaluation.cpcv")
        """Yield (train_dates, test_dates) for each fold, with purging and embargo.

        dates must be sorted ascending.
        Returns list of (train_indices, test_indices) tuples.
        """
        n = len(dates)
        if n < self.n_groups * 2:
            raise ValueError(f"Not enough dates ({n}) for {self.n_groups}-fold CPCV")

        group_size = n // self.n_groups
        splits = []
        logger.debug(f"CPCV split: {len(dates)} dates, {self.n_groups} groups, embargo={self.embargo_days}d")

        for i in range(self.n_groups):
            # Test set: group i
            test_start = i * group_size
            test_end = min((i + 1) * group_size, n) if i < self.n_groups - 1 else n

            # Train set: all groups strictly before test_start, purged
            # Purge: remove train observations within embargo_days of test start
            if i == 0:
                # First fold: no train data before test (walk-forward constraint)
                train_end = 0
                train_start = 0
            else:
                train_end = test_start - self.embargo_days
                train_start = 0

            # Only yield if we have both train and test data
            if train_end > 0 and test_end > test_start:
                train_indices = list(range(train_start, train_end))
                test_indices = list(range(test_start, test_end))
                splits.append((train_indices, test_indices))

        # If no splits generated (too few dates), create minimal split
        if not splits:
            logger.warning(f"CPCV: too few dates ({len(dates)}) for {self.n_groups} groups, falling back to 60/40 split")
            # Use first 60% as train, last 40% as test
            split_point = int(n * 0.6)
            splits = [(list(range(0, split_point - self.embargo_days)),
                       list(range(split_point, n)))]

        return splits


def compute_fold_icir(ic_series: pd.Series, dates: pd.DatetimeIndex,
                      train_idx: list, test_idx: list) -> dict:
    """Compute IS and OOS ICIR for one CPCV fold.

    Parameters
    ----------
    ic_series : pd.Series indexed by date, values are ICs
    dates : pd.DatetimeIndex of all available dates
    train_idx : list of int indices into dates for training
    test_idx : list of int indices into dates for testing

    Returns
    -------
    dict with keys: is_ic, is_icir, oos_ic, oos_icir, is_n, oos_n
    """
    train_dates = dates[train_idx]
    test_dates = dates[test_idx]

    is_vals = ic_series.reindex(train_dates).dropna()
    oos_vals = ic_series.reindex(test_dates).dropna()

    result = {"is_n": len(is_vals), "oos_n": len(oos_vals)}

    if len(is_vals) > 1:
        result["is_ic"] = float(is_vals.mean())
        result["is_icir"] = float(is_vals.mean() / is_vals.std()) if is_vals.std() > 0 else 0.0
    else:
        result["is_ic"] = 0.0
        result["is_icir"] = 0.0

    if len(oos_vals) > 1:
        result["oos_ic"] = float(oos_vals.mean())
        result["oos_icir"] = float(oos_vals.mean() / oos_vals.std()) if oos_vals.std() > 0 else 0.0
    else:
        result["oos_ic"] = 0.0
        result["oos_icir"] = 0.0

    return result
