"""组合构建器 — 资本自适应分配 (等权 / 得分倾斜 / 均值-方差)。

risk_aversion (Markowitz λ):
  不写入 config.yaml，不使用实例默认值。
  进入均值-方差分支时由 calibrate_risk_aversion() 实时网格搜索确定最优 λ。
  校准函数是模块级纯函数，不依赖 PortfolioConstructor 实例。
  来源: Markowitz (1952) 均值-方差框架, λ 决定收益/风险权衡。
  典型范围 1-10, 越低越激进 (追求收益), 越高越保守 (规避风险)。

交易成本感知 (§8.3, Grinold α − λ·TC 无交易区间):
  construct() 接受 current_lots + cost_model 后, 对各层产出的理想目标做
  换仓成本过滤: 持仓 A → 候选 B 的换股仅在预期收益差 ≥ λ × 实际成本时执行。
  E[Δr] = (z_B − z_A) × IC_eff × σ_daily × horizon (Grinold 基本法则),
  成本由 CostModel 实算 (含 ¥5 最低佣金, Nano 层一次全仓换股 ≈ 0.47%)。
  来源: Grinold & Kahn (2000) Ch.16; Gârleanu & Pedersen (2013)。
"""
from quant.utils.logger import get_logger
logger = get_logger("optimizer.portfolio")

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Optional


@dataclass
class TargetPortfolio:
    """目标持仓 — 整数手 (100 股的整数倍)。"""
    lots: pd.Series
    cash_reserve: float
    method: str
    total_value: float = 0.0
    tc_suppressed: int = 0   # §8.3: 被成本带拦截的换仓笔数 (0=未启用或无拦截)

    @property
    def positions(self) -> int:
        return int((self.lots > 0).sum())

    @property
    def invested(self) -> float:
        return self.total_value


from quant.config.constants import _require_cfg
LOT_SIZE = _require_cfg("backtest.lot_size")  # A股每手 100 股, ① 交易所规则

# ── §8.3 成本带参数 (config.yaml optimizer.*, 来源注释见 yaml) ──
_TC_LAMBDA = _require_cfg("optimizer.tc_lambda")
_TC_HORIZON = _require_cfg("optimizer.tc_horizon_days")
_TC_IC_REF = _require_cfg("optimizer.tc_ic_ref")
_TC_SIGMA_DAILY = _require_cfg("execution.default_daily_vol")  # 典型日波动率 (impact 模型同源)

_NORMAL = NormalDist()


def _ic_effective(ic_map) -> float:
    """从运行时 ic_map 估计截面 IC 强度: 因子 |IC| 的均值。

    ic_map 值可以是 float (factor_registry / factor_ic_daily) 或
    dict (含 ic_mean 键, compute_ic 风格)。缺失/全 NaN 时回退
    config optimizer.tc_ic_ref (校准值, 见 yaml 注释)。
    """
    vals = []
    for v in (ic_map or {}).values():
        if isinstance(v, dict):
            v = v.get("ic_mean", 0)
        if isinstance(v, (int, float)) and v == v:  # 排除 NaN
            vals.append(abs(float(v)))
    if vals:
        return sum(vals) / len(vals)
    return _TC_IC_REF


def _alpha_to_z(alpha: pd.Series) -> pd.Series:
    """截面 alpha → z-score: Blom 分位 (rank−3/8)/(n+1/4) 的正态逆累积。

    对 alpha 的任意单调/中性化变换稳健 (只用截面秩), 分位严格落在 (0,1)。
    来源: Blom (1958) plotting position; Grinold 基本法则要求 z 尺度输入。
    """
    n = len(alpha)
    ranks_asc = alpha.rank(method="first", ascending=True)  # 1 = 最低 alpha
    pct = (ranks_asc - 0.375) / (n + 0.25)
    return pct.map(_NORMAL.inv_cdf)

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
                "method": _require_cfg("optimizer.method"),
            }
        self.max_positions = config.get("max_positions")
        self.positions_per_factor = config.get("positions_per_factor", _require_cfg("alpha.sleeve.positions_per_factor"))
        self.max_single = config.get("max_single_position")
        self.nano_cap = config.get("nano_cap", _require_cfg("optimizer.nano_cap"))
        self.micro_cap = config.get("micro_cap", _require_cfg("optimizer.micro_cap"))
        # small 层风险优化器: hrp | risk_parity (test-v299 §8.2 接线, 原死配置)
        self.method = config.get("method") or _require_cfg("optimizer.method")

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
        current_lots: Optional[pd.Series] = None,
        cost_model=None,
        price_buffer: Optional[float] = None,  # test-v307: None=读config, 0=跳过(实时报价场景)
    ) -> TargetPortfolio:
        """资本自适应组合构建。

        根据 capital 与当前均价自动选择策略层级。
        进入均值-方差分支时实时校准 risk_aversion。

        current_lots: 当前持仓 (index=symbol, values=手数)。与 cost_model
          一起传入时启用 §8.3 成本带 — 理想目标与当前持仓的差量中,
          预期 alpha 收益 < λ × 实际换仓成本的换股被拦截 (保留原持仓)。
          None 或空 → 不启用, 行为与之前完全一致。
        """
        common = alpha.dropna().index.intersection(prices.dropna().index)
        if len(common) == 0:
            logger.warning(f"[portfolio] empty common universe, returning zero portfolio")
            return TargetPortfolio(pd.Series(dtype=int), capital, "equal_weight", 0.0)
        a = alpha.loc[common].sort_values(ascending=False)
        p = prices.loc[common]

        # v380: 价格缓冲不再虚增价格 — tier 判定和手数计算用真实价格。
        # 缓冲仅在成本带 (tc_band) 中用于成本估算, 不污染仓位计算。
        # 来源: QuantConnect/Backtrader 标准 — 价格缓冲只用于滑点/成本估算。
        n_top = min(self.max_positions, len(p))
        avg_price = float(p.loc[a.index[:n_top]].mean())
        tier = self._tier(capital, avg_price)
        logger.info(
            f"[portfolio] capital=¥{capital:,.0f} avg_price=¥{avg_price:.2f} "
            f"→ {tier} tier (nano_cap=¥{self.nano_cap:,.0f} micro_cap=¥{self.micro_cap:,.0f})"
        )

        if tier == "nano":
            try:
                result = self._rank_concentrated(a, p, capital)
            except ValueError:
                # v380: 价格缓冲已移除, 0 仓位只能来自真实价格过高 → 保留现金
                logger.warning("[portfolio] nano tier: capital too small for any lot, holding cash")
                return TargetPortfolio(pd.Series(dtype=int), capital, "equal_weight", 0.0)
        elif tier == "micro":
            result = self._score_weighted_rounding(a, p, capital)
            if result.lots.sum() == 0:
                logger.warning(
                    "[portfolio] micro tier produced 0 lots (capital=¥%s), "
                    "falling back to equal-weight greedy", f"{capital:,.0f}"
                )
                result = self._equal_weight_greedy(a, p, capital)
        else:  # small
            result = None
            # 风险优化器分发 (test-v299 §8.2): optimizer.method 选择 HRP 或 risk parity,
            # 均只依赖协方差; 失败/0 仓位 → 下方 Kelly/mean-variance 链兜底.
            if covariance is not None:
                if self.method == "hrp":
                    rp = self._hrp_lot(a, p, capital, covariance)
                else:
                    rp = self._risk_parity(a, p, capital, covariance)
                if rp.lots.sum() > 0:
                    result = rp
            if result is None:
                # Kelly if IC available, otherwise mean-variance
                if ic_map is not None:
                    result = self._kelly_greedy(a, p, capital, ic_map)
                elif covariance is None:
                    raise ValueError(
                        "Mean-variance tier requires covariance matrix. "
                        "Pass covariance= to construct()."
                    )
                else:
                    risk_aversion = calibrate_risk_aversion(
                        a, p, capital, covariance,
                        self.max_positions, self.max_single,
                    )
                    result = self._mean_variance_lot(a, p, capital, covariance, risk_aversion)

        # ── §8.3 成本带: 拦截不划算的换仓 (Nano层豁免 — 单票集中, 锁仓比换仓更贵) ──
        if current_lots is not None and len(current_lots) > 0 and cost_model is not None \
                and tier != "nano":
            result = self._apply_tc_band(result, current_lots, a, p, cost_model, ic_map)

        # ── P1-3: min_weight 过滤 — 剔除权重过低的噪声仓位 ──
        result = self._apply_min_weight(result, p, capital)

        return result

    def _apply_tc_band(
        self,
        ideal: TargetPortfolio,
        current_lots: pd.Series,
        alpha: pd.Series,
        prices: pd.Series,
        cost_model,
        ic_map: dict = None,
    ) -> TargetPortfolio:
        """Grinold α − λ·TC 无交易区间 (§8.3): 效益不足的换仓恢复为原持仓。

        算法:
          1. diff = 理想 − 当前 → 买单侧 (新增/加仓) 与卖单侧 (减仓/清仓)。
          2. 卖单中仅"仍在候选集内"的持仓可评估效益 (有 z 值);
             跌出候选集的持仓 (风险过滤/数据缺失) 无条件卖出, 不参与配对。
          3. 贪心配对: 最大买入金额 × 最弱 alpha 持仓, 逐手 chunk 判定:
             benefit = (z_B − z_A) × IC_eff × σ_daily × horizon × swap_val
             cost    = sell_cost(A) + buy_cost(B) − buy_val  (纯费用, CostModel 实算)
             benefit < λ × cost → 撤销该 chunk: B 减 chunk 手, A 恢复 chunk 手。
          4. 纯现金买入 (无卖单配对) 与无条件卖出不做门槛 — 只拦截"以卖养买"
             的换仓, 这是满仓 Nano 账户换手成本的主导来源。

        参数来源: config optimizer.tc_lambda / tc_horizon_days / tc_ic_ref,
        σ_daily 复用 execution.default_daily_vol; IC_eff 优先取运行时 ic_map
        的 |IC| 均值 (回测 walk-forward / 实盘 registry 均为实测值)。

        注: v380 起 prices 为原始价格 (price_buffer 不再虚增), 手数计算更准确。
        成本带判定仅依赖 alpha 差量和 CostModel, 不受价格缓冲影响。

        Args:
            ideal: 各层方法产出的理想目标。
            current_lots: 当前持仓 (index=symbol, values=手数)。
            alpha: 已排序的候选 alpha 序列 (与 prices 同 index)。
            prices: 候选价格序列 (原始价格, v380 起不含缓冲)。
            cost_model: CostModel 实例, 用于实算换仓费用。
            ic_map: 运行时 IC 权重 (可选, 缺失时 IC 取 config tc_ic_ref)。

        Returns:
            TargetPortfolio: 成本带过滤后的目标; tc_suppressed 记录拦截笔数。
        """
        z = _alpha_to_z(alpha)
        ic_eff = _ic_effective(ic_map)

        all_syms = ideal.lots.index.union(current_lots.index)
        tgt = ideal.lots.reindex(all_syms, fill_value=0).astype(int)
        cur = current_lots.reindex(all_syms, fill_value=0).astype(int)
        diff = tgt - cur

        sell_syms = [s for s in diff.index if diff[s] < 0 and s in z.index]
        sell_syms.sort(key=lambda s: z[s])                      # 最弱 alpha 先被换
        buy_syms = [s for s in diff.index if diff[s] > 0]
        buy_syms.sort(key=lambda s: -diff[s] * LOT_SIZE * prices.get(s, 0))  # 最大金额先配

        final = tgt.copy()
        sell_remaining = {s: int(-diff[s]) for s in sell_syms}
        n_suppressed = 0
        cost_saved = 0.0

        for b in buy_syms:
            b_remaining = int(diff[b])
            for a in sell_syms:
                if b_remaining <= 0:
                    break
                if sell_remaining.get(a, 0) <= 0:
                    continue
                chunk = min(b_remaining, sell_remaining[a])
                shares = chunk * LOT_SIZE
                buy_val = shares * prices[b]
                sell_val = shares * prices[a]
                swap_val = min(buy_val, sell_val)
                benefit = float(z[b] - z[a]) * ic_eff * _TC_SIGMA_DAILY * _TC_HORIZON * swap_val
                cost = (cost_model.sell_cost(prices[a], shares)
                        + cost_model.buy_cost(prices[b], shares) - buy_val)
                if benefit < _TC_LAMBDA * cost:
                    final[b] -= chunk
                    final[a] += chunk
                    n_suppressed += 1
                    cost_saved += cost
                    logger.info(
                        "[tc_band] swap suppressed: %s→%s %d手 "
                        "(benefit=¥%.2f < λ×cost=¥%.2f, Δz=%.2f)",
                        a, b, chunk, benefit, _TC_LAMBDA * cost, float(z[b] - z[a]),
                    )
                # 无论是否拦截, 该 chunk 的卖单额度都消费掉, 避免重复配对
                b_remaining -= chunk
                sell_remaining[a] -= chunk

        final = final[final > 0]
        invested = float((final * prices.loc[final.index] * LOT_SIZE).sum()) if len(final) else 0.0
        cash = round(ideal.cash_reserve + ideal.total_value - invested, 2)
        if n_suppressed:
            logger.info(
                "[tc_band] %d swap(s) suppressed, est. cost saved=¥%.2f "
                "(ic_eff=%.4f, λ=%.2f, horizon=%dd)",
                n_suppressed, cost_saved, ic_eff, _TC_LAMBDA, _TC_HORIZON,
            )
        return TargetPortfolio(final, cash, ideal.method, invested, tc_suppressed=n_suppressed)

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

    @staticmethod
    def _apply_min_weight(
        result: TargetPortfolio, prices: pd.Series, capital: float
    ) -> TargetPortfolio:
        """P1-3: 剔除权重过低的噪声仓位 (min_weight).

        单只持仓市值 < capital × min_weight → 清仓, 回收现金。
        阈值从 config optimizer.min_weight 读取 (默认 0.01 = 1%).
        """
        min_wt = _require_cfg("optimizer.min_weight")
        if min_wt <= 0 or result.lots.sum() == 0:
            return result
        threshold = capital * min_wt
        keep_mask = pd.Series(True, index=result.lots.index)
        reclaimed = 0.0
        for sym in result.lots.index:
            if result.lots[sym] <= 0:
                continue
            pos_value = result.lots[sym] * prices.get(sym, 0) * LOT_SIZE
            # 用原始价重算 (prices 可能是 buffered 价, 但 min_weight 按 nominal 判断即可)
            if pos_value < threshold:
                keep_mask[sym] = False
                reclaimed += pos_value
                logger.info(
                    f"[portfolio] min_weight trim: {sym} ¥{pos_value:,.0f} "
                    f"< {min_wt*100:.0f}% threshold (¥{threshold:,.0f})"
                )
        trimmed_lots = result.lots[keep_mask]
        if len(trimmed_lots) == len(result.lots):
            return result  # no trimming occurred
        adjusted_cash = result.cash_reserve + reclaimed
        trimmed_val = (trimmed_lots * prices.loc[trimmed_lots.index] * LOT_SIZE).sum() if len(trimmed_lots) > 0 else 0
        return TargetPortfolio(trimmed_lots, adjusted_cash, result.method, trimmed_val, result.tc_suppressed)

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

    def _hrp_lot(
        self,
        alpha: pd.Series,
        prices: pd.Series,
        capital: float,
        covariance: pd.DataFrame,
    ) -> TargetPortfolio:
        """HRP 层次风险平价 + 整手离散化。
        来源: De Prado (2016) "Building Diversified Portfolios that
              Outperform Out-of-Sample", JPM — 不逆协方差矩阵,
              层次聚类 + 递归二分分配风险预算, 高维截面比 mean-variance 稳。
        选股沿用 alpha 降序 top-N, 权重由协方差驱动 (与 _risk_parity 同范式)。
        """
        from quant.optimizer.hrp import hrp_weights
        common = [s for s in alpha.index if s in covariance.index and s in prices.index]
        n = min(self.max_positions, len(common))
        if n < 2:
            return TargetPortfolio(pd.Series(dtype=int), round(capital, 2), "hrp", 0.0)
        top = common[:n]
        p = prices.loc[top]
        Sigma = covariance.loc[top, top].values
        w_cont = hrp_weights(Sigma)
        w_cont = _iterative_clip(w_cont, self.max_single)
        lots = pd.Series(0, index=top, dtype=int)
        cash = capital
        for i, sym in enumerate(top):
            alloc = capital * w_cont[i]
            n_lots = int(alloc / (p[sym] * LOT_SIZE))
            if n_lots > 0:
                cost = n_lots * p[sym] * LOT_SIZE
                if cost <= cash:
                    lots[sym] = n_lots
                    cash -= cost
        total_value = (lots * p * LOT_SIZE).sum()
        return TargetPortfolio(lots[lots > 0], round(cash, 2), "hrp", total_value)

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
        # _iterative_clip 返回 numpy array; 转回 Series 保持 index 语义
        if isinstance(w, np.ndarray):
            w = pd.Series(w, index=sigmas.index)
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
