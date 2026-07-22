"""组合构建器 — 资本自适应分配 (等权 / 得分倾斜 / 均值-方差)。

risk_aversion (Markowitz λ):
  不写入 config.yaml，不使用实例默认值。
  进入均值-方差分支时由 calibrate_risk_aversion() 实时网格搜索确定最优 λ。
  校准函数是模块级纯函数，不依赖 PortfolioConstructor 实例。
  来源: Markowitz (1952) 均值-方差框架, λ 决定收益/风险权衡。
  典型范围 1-10, 越低越激进 (追求收益), 越高越保守 (规避风险)。
"""
from quant.utils.logger import get_logger
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


from quant.config.constants import _require_cfg
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
        inv_Sigma = np.linalg.pinv(Sigma)
        logger.warning("[calibrate] near-singular covariance, using pseudo-inverse")

    best_lambda = 2.0
    best_sharpe = -np.inf

    for lam in _CALIBRATION_GRID:
        w_raw = inv_Sigma @ alpha_vec / lam
        w_raw = np.maximum(w_raw, 0)
        if w_raw.sum() <= 0:
            continue
        w = w_raw / w_raw.sum()
        w = _iterative_clip(w, max_single)  # (2026-07-21 audit H6)

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


def _iterative_clip(w, max_single, max_iter=20):
    """迭代裁剪+重归一化: 保证所有权重 ≤ max_single 且 sum=1。

    算法: 反复裁剪超限权重, 剩余分配给未超限的。超限数单调递减, 保证收敛。
    来源: 2026-07-21 audit H6; De Prado & Lewis (2019) Ch.3.
    """
    import numpy as np
    w = np.asarray(w, dtype=float).copy()
    for _ in range(max_iter):
        over = w > max_single
        if not over.any():
            break
        if over.all():  # 全超限 → 等权 (避免死循环)
            return np.ones(len(w)) / len(w)
        w[over] = max_single
        s = w.sum()
        if s <= 0:
            return np.ones(len(w)) / len(w)
        w = w / s
    return w


class PortfolioConstructor:
    """资本自适应组合构建器。

    资本自适应分级 (阈值来自 config.yaml,
    来源 docs/reports/capital-segmentation-analysis-2026-07-15.md)。

      Nano 层:  capital < nano_cap (¥30,000)
        → 微型账户: 贪心等权, 集中持仓 1-3 只, 降低佣金占比
        → Kelly 不适用: 离散化误差 >30%, 使用纯等权

      Micro 层:  nano_cap ≤ capital < micro_cap (¥100,000)
        → 小型账户: 得分倾斜 + 整手舍入, 3-8 只股票

      Small 层:  capital ≥ micro_cap
        → 中型+: Risk Parity / Kelly 均值-方差, 8-20 只股票
        → 每次进入此分支时实时调用 calibrate_risk_aversion() 确定 λ

      来源: Markowitz (1952); Grinold & Kahn (2000) Ch.7;
            DeMiguel, Garlappi, Uppal (2009); 华泰金工 (2020)
    """

    def __init__(self, config: Optional[dict] = None):
        if config is None:
            config = {
                "max_positions": _require_cfg("risk.max_positions"),
                "positions_per_factor": _require_cfg("alpha.sleeve.positions_per_factor"),
                "max_single_position": _require_cfg("risk.max_single_position"),
                "nano_cap": _require_cfg("optimizer.nano_cap"),
                "micro_cap": _require_cfg("optimizer.micro_cap"),
            }
        self.max_positions = config.get("max_positions")
        self.positions_per_factor = config.get("positions_per_factor", _require_cfg("alpha.sleeve.positions_per_factor"))
        self.max_single = config.get("max_single_position")
        self.nano_cap = config.get("nano_cap", _require_cfg("optimizer.nano_cap"))
        self.micro_cap = config.get("micro_cap", _require_cfg("optimizer.micro_cap"))

    def _tier(self, capital: float, avg_price: float) -> str:
        """根据资金量判定组合优化层级。

        capital < nano_cap   → Nano 层: 贪心等权 (1-3 只)
        capital < micro_cap  → Micro 层: 得分倾斜 (3-8 只)
        否则                  → Small 层: 均值-方差 (8-20 只)
        """
        if capital < self.nano_cap:
            return "nano"
        elif capital < self.micro_cap:
            return "micro"
        else:
            return "small"

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
            f"→ {tier} tier (nano_cap=¥{self.nano_cap:,.0f} micro_cap=¥{self.micro_cap:,.0f})"
        )

        if tier == "nano":
            return self._rank_concentrated(a, p, capital)
        elif tier == "micro":
            result = self._score_weighted_rounding(a, p, capital)
            if result.lots.sum() == 0:
                logger.warning(
                    "[portfolio] micro tier produced 0 lots (capital=¥%s), "
                    "falling back to equal-weight greedy", f"{capital:,.0f}"
                )
                return self._equal_weight_greedy(a, p, capital)
            return result
        else:  # small
            # Risk parity first if covariance available
            if covariance is not None:
                result = self._risk_parity(a, p, capital, covariance)
                if result.lots.sum() > 0:
                    return result
            # Kelly if IC available, otherwise mean-variance
            if ic_map is not None:
                return self._kelly_greedy(a, p, capital, ic_map)
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
        """Kelly 头寸分配 (Small 层 ¥100K+ 专用)。

        ⚠️  ADR 032: Kelly 仅在 Small 层启用。Nano/Micro 层禁止。
        在低资本层引入 Kelly 离散化误差 >25%，已反复造成 0 仓位 bug。

        来源: Kelly (1956), Fractional Kelly per Ralph Vince (1990).
        当 ic_map 为 None 或全零时退化为贪心等权 (向后兼容).
        """
        from quant.optimizer.kelly import compute_lot_allocation
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

    def _rank_concentrated(
        self, alpha: pd.Series, prices: pd.Series, capital: float,
    ) -> TargetPortfolio:
        """排名集中: 按 alpha 降序逐只满仓买入, 直至资金不足买下一手。

        算法:
          for sym in alpha_rank_order:
              买入 max_lots = int(cash // (price × LOT_SIZE))
              扣减 cash
              if 剩余资金 < 最便宜候选 × LOT_SIZE: break

        适用: Nano 层 (capital < nano_cap).
        设计依据:
          - Grinold & Kahn (2000) 基本面法则: N=1-2 时需最大化 IC, 降低佣金侵蚀
          - Kirby & Ostdiek (2012): 换手成本 > 分散化收益时, 应集中持仓
          - capital-segmentation-analysis-2026-07-15 C3: 单笔<¥10K 交易成本占比>100% alpha,
            集中持仓减少交易笔数是唯一解
          - 与 _equal_weight_greedy 的区别: 本方法按排名依次满仓 (alpha优先),
            而非轮转均分 (分散化优先)
        """
        n_candidates = min(self.max_positions, len(alpha))
        if n_candidates == 0:
            return TargetPortfolio(pd.Series(dtype=int), capital, "rank_concentrated", 0.0)

        # 最便宜候选的一手成本 — 提前终止条件
        cheapest_lot = prices.loc[alpha.index[:n_candidates]].min() * LOT_SIZE

        lots = pd.Series(0, index=alpha.index, dtype=int)
        cash = capital
        symbol_order = list(alpha.index[:n_candidates])

        for sym in symbol_order:
            cost_per_lot = prices[sym] * LOT_SIZE
            if cash < cost_per_lot:
                continue  # 买不起这只, 试下一只
            max_lots = int(cash // cost_per_lot)
            lots[sym] = max_lots
            cash -= max_lots * cost_per_lot
            if cash < cheapest_lot:
                break  # 剩余资金不够买任何候选

        total_value = (lots * prices * LOT_SIZE).sum()
        if lots.sum() == 0:
            raise ValueError(
                f"rank_concentrated produced 0 lots: "
                f"n_candidates={n_candidates} capital={capital:,.0f} "
                f"cheapest_lot={cheapest_lot:,.0f} "
                f"top3_prices={prices.loc[alpha.index[:min(3,n_candidates)]].tolist()}"
            )
        return TargetPortfolio(lots[lots > 0], round(cash, 2), "rank_concentrated", total_value)

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
        weights = _iterative_clip(weights, self.max_single)  # (2026-07-21 audit H6)
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
                except np.linalg.LinAlgError:
                    inv_Sigma = np.linalg.pinv(Sigma)
                    logger.warning("[mean_variance_lot] near-singular covariance, using pseudo-inverse")
                w_raw = inv_Sigma @ alpha_vec / risk_aversion
                w_raw = np.maximum(w_raw, 0)
                if w_raw.sum() > 0:
                    w_cont = w_raw / w_raw.sum()
                    w_cont = _iterative_clip(w_cont, self.max_single)
                else:
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
        w = _iterative_clip(w, self.max_single)  # (2026-07-21 audit H6)
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
