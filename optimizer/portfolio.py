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


from config.constants import _require_cfg
LOT_SIZE = _require_cfg("backtest.lot_size")  # A股每手 100 股, ① 交易所规则


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

    资本自适应分级 (阈值来自 config.yaml, 来源 ARCHITECTURE.md v3.0):

      贪心等权:   capital < greedy_cap (¥20,000)
        → 微型账户: 集中持仓, 逐手买入得分最高的股票, 降低佣金占比

      得分倾斜:   greedy_cap ≤ capital < weighted_cap (¥100,000)
        → 小型账户: 按得分比例分配 → 整手舍入
        → 若产出 0 仓位则自动回退到贪心等权

      均值-方差:  capital ≥ weighted_cap
        → 中型+: Markowitz 均值-方差优化 + 整手离散化
        → 每次进入此分支时实时调用 calibrate_risk_aversion() 确定 λ
        → 来源: Markowitz (1952); Grinold & Kahn (2000) Chapter 7
    """

    def __init__(self, config: Optional[dict] = None):
        if config is None:
            config = {
                "max_positions": _require_cfg("risk.max_positions"),
                "max_single_position": _require_cfg("risk.max_single_position"),
                "greedy_cap": _require_cfg("optimizer.greedy_cap"),
                "weighted_cap": _require_cfg("optimizer.weighted_cap"),
            }
        self.max_positions = config.get("max_positions")
        self.max_single = config.get("max_single_position")
        self.greedy_cap = config.get("greedy_cap", _require_cfg("optimizer.greedy_cap"))
        self.weighted_cap = config.get("weighted_cap", _require_cfg("optimizer.weighted_cap"))

    def _tier(self, capital: float, avg_price: float) -> str:
        """根据资金量判定组合优化层级 (固定阈值, 来源 ARCHITECTURE.md v3.0)。

        capital < greedy_cap  → 贪心逐手买入 (微型账户: 集中持仓, 降低佣金占比)
        capital < weighted_cap → 得分配比 (小型账户: 适度分散)
        否则 → 均值-方差 (中型+: 分散化收益大于成本)
        """
        if capital < self.greedy_cap:
            return "greedy"
        elif capital < self.weighted_cap:
            return "weighted"
        else:
            return "mean_var"

    def construct(
        self,
        alpha: pd.Series,
        prices: pd.Series,
        capital: float,
        covariance: Optional[pd.DataFrame] = None,
        ic_map: dict = None,
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
            # Try risk parity first if covariance available
            if covariance is not None:
                result = self._risk_parity(a, p, capital, covariance)
                if result.lots.sum() > 0:
                    return result
            return self._kelly_greedy(a, p, capital, ic_map)
        elif tier == "weighted":
            result = self._score_weighted_rounding(a, p, capital)
            # Safety net: weighted produces 0 lots → fall back to greedy
            if result.lots.sum() == 0:
                logger.warning(
                    "[portfolio] weighted tier produced 0 lots (capital=¥%s), "
                    "falling back to Kelly greedy", f"{capital:,.0f}"
                )
                return self._kelly_greedy(a, p, capital, ic_map)
            return result
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

    def _kelly_greedy(
        self, alpha: pd.Series, prices: pd.Series, capital: float, ic_map: dict = None,
    ) -> TargetPortfolio:
        """Kelly 头寸分配: 用 Kelly 分数替代等权，集中资金到最强信号。

        来源: Kelly (1956), Fractional Kelly per Ralph Vince (1990).
        当 ic_map 为 None 或全零时退化为贪心等权 (向后兼容).
        """
        from optimizer.kelly import compute_lot_allocation
        n_stocks = min(self.max_positions, len(alpha))
        if n_stocks == 0:
            return TargetPortfolio(pd.Series(dtype=int), capital, "kelly_greedy", 0.0)
        lots, cash = compute_lot_allocation(
            alpha, prices, capital, ic_map, self.max_positions, LOT_SIZE
        )
        total_value = (lots * prices.loc[lots.index] * LOT_SIZE).sum()
        if lots.sum() == 0:
            raise ValueError(
                f"Kelly greedy produced 0 lots: "
                f"n_stocks={n_stocks} capital={capital:,.0f} "
                f"top3_prices={prices.loc[alpha.index[:min(3,n_stocks)]].tolist()} "
                f"cheapest_lot={prices.loc[alpha.index[:n_stocks]].min() * LOT_SIZE:,.0f}"
            )
        return TargetPortfolio(lots, cash, "kelly_greedy", total_value)

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

    def _risk_parity(self, alpha, prices, capital, covariance):
        """Risk parity: w_i = (1/sigma_i) / sum(1/sigma_j)"""
        common = [s for s in alpha.index if s in covariance.index and s in prices.index]
        if len(common) < 2:
            return self._kelly_greedy(alpha, prices, capital)
        n = min(self.max_positions, len(common))
        top = common[:n]
        sigmas = pd.Series({s: max(abs(covariance.loc[s, s]), 1e-10)**0.5
                            for s in top if s in covariance.index})
        if sigmas.empty or sigmas.sum() == 0:
            return self._kelly_greedy(alpha, prices, capital)
        w = (1.0 / sigmas) / (1.0 / sigmas).sum()
        w = w.clip(upper=self.max_single)
        w = w / w.sum()
        lots = pd.Series(0, index=top, dtype=int)
        cash = capital
        for sym in top:
            if sym in w.index:
                alloc = capital * w[sym]
                n_lots = int(alloc / (prices[sym] * LOT_SIZE))
                if n_lots > 0:
                    cost = n_lots * prices[sym] * LOT_SIZE
                    if cost <= cash:
                        lots[sym] = n_lots
                        cash -= cost
        tv = (lots * prices.loc[top] * LOT_SIZE).fillna(0).sum()
        if lots.sum() == 0:
            return self._kelly_greedy(alpha, prices, capital)
        return TargetPortfolio(lots[lots > 0], round(cash, 2), "risk_parity", tv)

    @classmethod
    def from_config(cls) -> "PortfolioConstructor":
        """从 config.yaml 创建实例。"""
        return cls()
