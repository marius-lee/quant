"""因子抽象基类 — 定义 Factor 的 compute / evaluate 协议。

每个因子实现 compute() 在给定截面上计算因子值，
evaluate() 方法由基类统一提供（委托 evaluate.py）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np


@dataclass
class FactorResult:
    """单个因子在某日截面的计算结果。
    
    values: index=symbol, value=factor_score (未标准化)
    date:   计算日期
    name:   因子名, 例 "momentum_20d"
    """
    name: str
    date: str
    values: pd.Series
    category: str = ""


@dataclass
class FactorStats:
    """因子评估统计量。
    
    来源: ② Jegadeesh & Titman (1993); Grinold & Kahn (2000) Chapter 7
    """
    name: str
    rank_ic_mean: float = 0.0        # 截面 Rank IC 均值
    rank_ic_std: float = 0.0         # 截面 Rank IC 标准差
    ic_ir: float = 0.0               # IC / IC_std (Information Ratio)
    ic_decay: dict = field(default_factory=dict)  # {horizon_days: IC}
    n_periods: int = 0               # 有效截面数


class Factor(ABC):
    """因子抽象基类。
    
    子类必须实现:
      - name: str          因子名, 全项目唯一
      - category: str      因子类别: momentum|reversal|volatility|volume|liquidity|skewness
      - compute(data, date) → FactorResult
    
    可选覆盖:
      - metadata: dict     因子元信息 (来源、参数等)
    """

    name: str = ""
    category: str = ""
    metadata: dict = {}

    @abstractmethod
    def compute(self, data: pd.DataFrame, date: str) -> FactorResult:
        """在指定日期截面上计算因子值。
        
        data: 由 DataStore.get_daily() 返回的宽表 DataFrame,
              MultiIndex columns: (field, symbol), index=date.
              field 包含: close, volume, amount, turnover
        
        date: 计算日期 (YYYY-MM-DD), 必须存在于 data.index 中
        
        返回: FactorResult(values: index=symbol 的 Series)
        """
        ...

    def evaluate(
        self,
        factor_values: pd.Series,
        forward_returns: pd.Series,
        decay_horizons: Optional[list] = None,
    ) -> FactorStats:
        """评估因子在历史上的预测能力（委托 evaluate.py）。
        
        factor_values: index=(date,symbol) MultiIndex, 因子值
        forward_returns: index=(date,symbol) MultiIndex, 前瞻收益率
        decay_horizons: IC 衰减分析窗口, 默认 [1, 5, 20]
        """
        from factor.evaluate import evaluate_factor
        return evaluate_factor(
            self, factor_values, forward_returns, decay_horizons
        )

    def __repr__(self):
        return f"Factor({self.name}, cat={self.category})"
