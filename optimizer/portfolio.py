"""组合构建器 — 资本自适应分配 (等权 / 得分倾斜 / 均值-方差)。"""
from utils.logger import get_logger
logger = get_logger("optimizer.portfolio")

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TargetPortfolio:
    """目标持仓 — 整数手 (100 股的整数倍)。"""
    lots: pd.Series
    cash_reserve: float
    method: str
    total_value: float = 0.0

    @property
    def positions(self) -> int:
        # REVIEWED: 2026-07-05 — .sum() on pd.Series returns numpy.int64,
        # which is not JSON-serializable by Python 3.14 simplejson.
        # Cast to native int.
        return int((self.lots > 0).sum())

    @property
    def invested(self) -> float:
        """已投入资金 = 持仓总市值（total_value 已是 手数×价格×100 股）。"""
        return self.total_value


from config.loader import get as _cfg
LOT_SIZE = _cfg("backtest.lot_size")  # A股每手 100 股, ① 交易所规则


class PortfolioConstructor:
    """资本自适应组合构建器。

    三档策略:
      < ¥20,000:  等权贪心 — Top N 等权, 每轮给得分最高的未满仓股票加 1 手
      ¥20k-100k:  得分倾斜 — 按得分比例分配资金 → 整手舍入 → 修正余数
      > ¥100,000: 均值-方差 — 连续权重 → 整数规划 → 逐手分配
    """

    def __init__(self, config: Optional[dict] = None):
        if config is None:
            from config.loader import get as cfg
            config = {
                "equal_weight_cap": cfg("optimizer.equal_weight_cap", 20000),
                "weighted_cap": cfg("optimizer.weighted_cap", 100000),
                "max_positions": cfg("risk.max_positions", 20),
                "max_single_position": cfg("risk.max_single_position", 0.10),
                "risk_aversion": cfg("optimizer.risk_aversion", 2.0),
            }
        self.equal_weight_cap = config.get("equal_weight_cap", 20000)
        self.weighted_cap = config.get("weighted_cap", 100000)
        self.max_positions = config.get("max_positions", 20)
        self.max_single = config.get("max_single_position", 0.10)
        self.risk_aversion = config.get("risk_aversion", 2.0)

    def construct(
        self,
        alpha: pd.Series,
        prices: pd.Series,
        capital: float,
        covariance: Optional[pd.DataFrame] = None,
    ) -> TargetPortfolio:
        """资本自适应组合构建。"""
        common = alpha.dropna().index.intersection(prices.dropna().index)
        if len(common) == 0:
            logger.warning(f"[portfolio] empty common universe, returning zero portfolio")
            return TargetPortfolio(pd.Series(dtype=int), capital, "equal_weight", 0.0)
        a = alpha.loc[common].sort_values(ascending=False)
        logger.info(f"[portfolio] capital=¥{capital:,.0f} → {"greedy" if capital < self.equal_weight_cap else "weighted" if capital < self.weighted_cap else "mean_var"} tier")
        p = prices.loc[common]
        if capital < self.equal_weight_cap:
            return self._equal_weight_greedy(a, p, capital)
        elif capital < self.weighted_cap:
            return self._score_weighted_rounding(a, p, capital)
        else:
            return self._mean_variance_lot(a, p, capital, covariance)

    def _equal_weight_greedy(
        self, alpha: pd.Series, prices: pd.Series, capital: float,
    ) -> TargetPortfolio:
        """贪心等权: 每轮给得分最高的未满仓股票加 1 手。
        来源: ④ 用户确认 — 小资金整手约束极度刚性, 严格等权是唯一稳定解
        """
        n_stocks = min(self.max_positions, len(alpha))
        if n_stocks == 0:
            return TargetPortfolio(pd.Series(dtype=int), capital, "equal_weight", 0.0)
        lots = pd.Series(0, index=alpha.index, dtype=int)
        cash = capital
        symbol_order = list(alpha.index[:n_stocks])
        max_lots_per = max(1, int(capital / (n_stocks * prices.iloc[:n_stocks].mean() * LOT_SIZE)) + 1)
        for _ in range(max_lots_per):
            for sym in symbol_order:
                cost = prices[sym] * LOT_SIZE
                if lots[sym] < max_lots_per and cash >= cost:
                    lots[sym] += 1
                    cash -= cost
        total_value = (lots * prices * LOT_SIZE).sum()
        return TargetPortfolio(lots[lots > 0], round(cash, 2), "equal_weight", total_value)

    def _score_weighted_rounding(
        self, alpha: pd.Series, prices: pd.Series, capital: float,
    ) -> TargetPortfolio:
        """得分倾斜 + 整数舍入。
        来源: ④ 用户确认 — 每只可买 10-20 手, 按得分倾斜权重后用整数规划修正
        """
        n_stocks = min(self.max_positions, len(alpha))
        top = alpha.iloc[:n_stocks]
        p = prices.loc[top.index]
        scores = top.values - top.min()
        if scores.sum() == 0:
            scores = np.ones(n_stocks)
        weights = scores / scores.sum()
        weights = np.minimum(weights, self.max_single)
        weights = weights / weights.sum()
        lots = pd.Series(0, index=top.index, dtype=int)
        cash = capital
        for i, sym in enumerate(top.index):
            alloc = capital * weights[i]
            n_lots = int(alloc / (p[sym] * LOT_SIZE))
            if n_lots > 0:
                cost = n_lots * p[sym] * LOT_SIZE
                if cost <= cash:
                    lots[sym] = n_lots
                    cash -= cost
        total_value = (lots * p * LOT_SIZE).sum()
        return TargetPortfolio(lots[lots > 0], round(cash, 2), "score_weighted", total_value)

    def _mean_variance_lot(
        self,
        alpha: pd.Series,
        prices: pd.Series,
        capital: float,
        covariance: Optional[pd.DataFrame] = None,
    ) -> TargetPortfolio:
        """均值-方差优化 + 整手离散化。
        来源: ② Markowitz (1952); ② Grinold & Kahn (2000) Chapter 7
        """
        n_stocks = min(self.max_positions, len(alpha))
        top = alpha.iloc[:n_stocks]
        p = prices.loc[top.index]
        if covariance is not None:
            common_cov = [s for s in top.index if s in covariance.index]
            if len(common_cov) >= 3:
                alpha_vec = top.loc[common_cov].values
                Sigma = covariance.loc[common_cov, common_cov].values
                try:
                    inv_Sigma = np.linalg.inv(Sigma)
                    w_raw = inv_Sigma @ alpha_vec / self.risk_aversion
                    w_raw = np.maximum(w_raw, 0)
                    if w_raw.sum() > 0:
                        w_cont = w_raw / w_raw.sum()
                        w_cont = np.minimum(w_cont, self.max_single)
                        w_cont = w_cont / w_cont.sum()
                    else:
                        w_cont = np.ones(len(common_cov)) / len(common_cov)
                    symbols = common_cov
                except np.linalg.LinAlgError:
                    w_cont = np.ones(len(common_cov)) / len(common_cov)
                    symbols = common_cov
            else:
                w_cont = np.ones(n_stocks) / n_stocks
                symbols = top.index.tolist()
        else:
            w_cont = np.ones(n_stocks) / n_stocks
            symbols = top.index.tolist()
        lots = pd.Series(0, index=top.index, dtype=int)
        cash = capital
        for i, sym in enumerate(symbols):
            alloc = capital * w_cont[i]
            n_lots = int(alloc / (p[sym] * LOT_SIZE))
            if n_lots > 0:
                cost = n_lots * p[sym] * LOT_SIZE
                if cost <= cash:
                    lots[sym] = n_lots
                    cash -= cost
        total_value = (lots * p * LOT_SIZE).sum()
        return TargetPortfolio(lots[lots > 0], round(cash, 2), "mean_variance", total_value)

    @classmethod
    def from_config(cls) -> "PortfolioConstructor":
        """从 config.yaml 创建实例。"""
        return cls()
