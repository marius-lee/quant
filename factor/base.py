"""因子基类和公共操作。

因子计算流程: 原始数据 → 去极值 → 缺失值填充 → 标准化
"""

from abc import ABC, abstractmethod

import pandas as pd


def winsorize_mad(series: pd.Series, n: float = 3.0) -> pd.Series:
    """MAD 去极值: 偏离中位数超过 n 倍 MAD 的值被截断"""
    med = series.median()
    mad = (series - med).abs().median()
    if mad == 0:
        return series
    upper = med + n * 1.4826 * mad
    lower = med - n * 1.4826 * mad
    return series.clip(lower, upper)


def normalize_zscore(df: pd.DataFrame) -> pd.DataFrame:
    """截面标准化（每期横截面上 Z-Score）"""
    std = df.std(axis=1).replace(0, 1.0)
    return df.sub(df.mean(axis=1), axis=0).div(std, axis=0)



class BaseFactor(ABC):
    """因子基类"""

    def __init__(self, na_fill: str = "median"):
        self.na_fill = na_fill

    @abstractmethod
    def compute(self, data: dict) -> pd.DataFrame:
        """计算因子值。返回 (dates × stocks) DataFrame"""
        ...

    def process(self, factor_df: pd.DataFrame) -> pd.DataFrame:
        """后处理流水线: 去极值 → 填充 → 标准化"""
        # 1. 去极值
        factor_df = factor_df.apply(
            lambda row: winsorize_mad(row), axis=1
        )
        # 2. 缺失值填充
        if self.na_fill == "median":
            factor_df = factor_df.T.fillna(factor_df.median(axis=1)).T
        elif self.na_fill == "zero":
            factor_df = factor_df.fillna(0)
        # 3. 截面标准化
        factor_df = normalize_zscore(factor_df)
        return factor_df
