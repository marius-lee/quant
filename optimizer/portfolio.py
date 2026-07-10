"""组合构建器 — 资本自适应分配 (等权 / 得分倾斜 / 均值-方差)。

risk_aversion (Markowitz λ):
  不写入 config.yaml，不使用实例默认值。
  进入均值-方差分支时由 calibrate_risk_aversion() 实时网格搜索确定最优 λ。
  校准函数是模块级纯函数，不依赖 PortfolioConstructor 实例。
  来源: Markowitz (1952) 均值-方差框架, λ 决定收益/风险权衡。
  典型范围 1-10, 越低越激进 (追求收益), 越高越保守 (规避风险)。
"""
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
        return int((self.lots > 0).sum())

    @property
    def invested(self) -> float:
        return self.total_value


from config.loader import get as _cfg
LOT_SIZE = _cfg("backtest.lot_size")  # A股每手 100 股, ① 交易所规则


# ── risk_aversion 校准网格 ──
# 来源: Markowitz (1952) 框架下 λ 典型范围 1-10。
# 0.5 为极端激进, 10.0 为极端保守, 网格覆盖全范围。
_CALIBRATION_GRID = [0.5, 1.0, 2.0, 5.0, 10.0]


def calibrate_risk_aversion(
    alpha: pd.Series,
    prices: pd.Series,
    capital: float,
    covariance: pd.DataFrame,
    max_positions: int = 20,
    max_single: float = 0.05,
) -> float:
    """网格搜索最优 Markowitz 风险厌恶系数 λ。

    方法:
      对 _CALIBRATION_GRID 中每个候选 λ:
        1. 取 alpha 前 max_positions 只股票
        2. 用协方差矩阵做均值-方差优化: w = inv(Σ) @ α / λ → normalize
        3. 计算组合预期收益 μ_p = w'α, 标准差 σ_p = sqrt(w'Σw)
        4. 计算 Sharpe = μ_p / σ_p (近似, 未减无风险利率)
      选 Sharpe 最大的 λ。

    返回:
      最优 λ (float)。若数据不足无法校准, 返回 2.0。
    """
    n_stocks = min(max_positions, len(alpha))
    top = alpha.iloc[:n_stocks]
    common = [s for s in top.index if s in covariance.index]
    if len(common) < 3:
        logger.warning(
            "[calibrate] insufficient common stocks in covariance (%d < 3), "
            "cannot calibrate — returning conservative λ=2.0", len(common)
        )
        return 2.0

    alpha_vec = top.loc[common].values
    Sigma = covariance.loc[common, common].values

    try:
        inv_Sigma = np.linalg.inv(Sigma)
    except np.linalg.LinAlgError:
        logger.warning("[calibrate] singular covariance matrix — returning conservative λ=2.0")
        return 2.0

    best_lambda = 2.0
    best_sharpe = -np.inf

    for lam in _CALIBRATION_GRID:
        w_raw = inv_Sigma @ alpha_vec / lam
        w_raw = np.maximum(w_raw, 0)
        if w_raw.sum() <= 0:
            continue
        w = w_raw / w_raw.sum()
        w = np.minimum(w, max_single)
        w = w / w.sum()

        mu_p = np.dot(w, alpha_vec)
        sigma_p = np.sqrt(np.dot(w.T, np.dot(Sigma, w)))
        sharpe = mu_p / sigma_p if sigma_p > 0 else 0.0

        if sharpe > best_sharpe:
            best_sharpe = sharpe
            best_lambda = lam

    logger.info(
        "[calibrate] grid search complete: best λ=%.1f (Sharpe=%.4f) "
        "from grid %s", best_lambda, best_sharpe, _CALIBRATION_GRID
    )
    return best_lambda


class PortfolioConstructor:
    """资本自适应组合构建器。

    资本自适应分级 (阈值由资金量与股价实时决定, 无硬编码):

      贪心等权:   capital < 均价×lot_size×2
        → 资金太小 (连2手都买不起), 强制逐手买入得分最高的股票

      得分倾斜:   均价×lot_size×2 ≤ capital < 均价×lot_size×max_positions
        → 按得分比例分配 → 整手舍入

      均值-方差:  capital ≥ 均价×lot_size×max_positions
        → Markowitz 均值-方差优化 + 整手离散化
        → 每次进入此分支时实时调用 calibrate_risk_aversion() 确定 λ
        → 来源: Markowitz (1952); Grinold & Kahn (2000) Chapter 7
    """

    def __init__(self, config: Optional[dict] = None):
        if config is None:
            config = {
                "max_positions": _cfg("risk.max_positions"),
                "max_single_position": _cfg("risk.max_single_position"),
            }
        self.max_positions = config.get("max_positions")
        self.max_single = config.get("max_single_position")

    def _tier(self, capital: float, avg_price: float) -> str:
        """根据资金量与当前均价自动判定组合优化层级。

        lot_cost = avg_price × LOT_SIZE  — 买1手需要的资金
        capital < lot_cost × 2  → greedy
        capital < lot_cost × max_positions → weighted
        否则 → mean_var
        """
        lot_cost = avg_price * LOT_SIZE
        if capital < lot_cost * 2:
            return "greedy"
        elif capital < lot_cost * self.max_positions:
            return "weighted"
        else:
            return "mean_var"

    def construct(
        self,
        alpha: pd.Series,
        prices: pd.Series,
        capital: float,
        covariance: Optional[pd.DataFrame] = None,
    ) -> TargetPortfolio:
        """资本自适应组合构建。

        根据 capital 与当前均价自动选择策略层级。
        进入均值-方差分支时实时校准 risk_aversion。
        """
        common = alpha.dropna().index.intersection(prices.dropna().index)
        if len(common) == 0:
            logger.warning(f"[portfolio] empty common universe, returning zero portfolio")
            return TargetPortfolio(pd.Series(dtype=int), capital, "equal_weight", 0.0)
        a = alpha.loc[common].sort_values(ascending=False)
        p = prices.loc[common]

        n_top = min(self.max_positions, len(p))
        avg_price = float(p.loc[a.index[:n_top]].mean())
        tier = self._tier(capital, avg_price)
        logger.info(
            f"[portfolio] capital=¥{capital:,.0f} avg_price=¥{avg_price:.2f} "
            f"→ {tier} tier "
            f"(threshold_greedy=¥{avg_price * LOT_SIZE * 2:,.0f}, "
            f"threshold_mv=¥{avg_price * LOT_SIZE * self.max_positions:,.0f})"
        )

        if tier == "greedy":
            return self._equal_weight_greedy(a, p, capital)
        elif tier == "weighted":
            return self._score_weighted_rounding(a, p, capital)
        else:
            # 均值-方差分支: 实时校准 risk_aversion
            if covariance is None:
                raise ValueError(
                    "Mean-variance tier requires covariance matrix. "
                    "Pass covariance= to construct()."
                )
            risk_aversion = calibrate_risk_aversion(
                a, p, capital, covariance,
                self.max_positions, self.max_single,
            )
            return self._mean_variance_lot(a, p, capital, covariance, risk_aversion)

    def _equal_weight_greedy(
        self, alpha: pd.Series, prices: pd.Series, capital: float,
    ) -> TargetPortfolio:
        """贪心等权: 每轮给得分最高的未满仓股票加 1 手。"""
        n_stocks = min(self.max_positions, len(alpha))
        if n_stocks == 0:
            return TargetPortfolio(pd.Series(dtype=int), capital, "equal_weight", 0.0)
        lots = pd.Series(0, index=alpha.index, dtype=int)
        cash = capital
        symbol_order = list(alpha.index[:n_stocks])
        max_lots_per = max(1, int(capital / (n_stocks * prices.loc[alpha.index[:n_stocks]].mean() * LOT_SIZE)) + 1)
        for _ in range(max_lots_per):
            for sym in symbol_order:
                cost = prices[sym] * LOT_SIZE
                if lots[sym] < max_lots_per and cash >= cost:
                    lots[sym] += 1
                    cash -= cost
        total_value = (lots * prices * LOT_SIZE).sum()
        if lots.sum() == 0:
            raise ValueError(
                f"greedy produced 0 lots: "
                f"n_stocks={n_stocks} capital={capital:,.0f} "
                f"max_lots_per={max_lots_per} "
                f"top3_prices={prices.loc[alpha.index[:min(3,n_stocks)]].tolist()} "
                f"cheapest_lot={prices.loc[alpha.index[:n_stocks]].min() * LOT_SIZE:,.0f}"
            )
        return TargetPortfolio(lots[lots > 0], round(cash, 2), "equal_weight", total_value)

    def _score_weighted_rounding(
        self, alpha: pd.Series, prices: pd.Series, capital: float,
    ) -> TargetPortfolio:
        """得分倾斜 + 整数舍入。"""
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
        covariance: Optional[pd.DataFrame],
        risk_aversion: float,
    ) -> TargetPortfolio:
        """均值-方差优化 + 整手离散化。
        来源: Markowitz (1952); Grinold & Kahn (2000) Chapter 7
        参数 risk_aversion 由 construct() 实时调用 calibrate_risk_aversion() 确定。
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
                    w_raw = inv_Sigma @ alpha_vec / risk_aversion
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
