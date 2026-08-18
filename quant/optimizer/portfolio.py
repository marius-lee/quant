"""组合构建器 — 资本自适应分配 (等权 / 得分倾斜 / 均值-方差)。

risk_aversion (Markowitz λ):
  不写入 config.yaml，不使用实例默认值。
  进入均值-方差分支时读取 config optimizer.risk_aversion (v535: 原网格恒选左边界, 删除)。
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
import hashlib


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
_DEFAULT_SIGMA_DAILY = _require_cfg("execution.default_daily_vol")  # 典型日波动率 fallback

# test-v397: 换手率约束 (Problem 9)
_MAX_TURNOVER = _require_cfg("optimizer.max_turnover_ratio")

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


def _stock_sigma(symbol: str, log_returns: pd.DataFrame = None) -> float:
    """从 log_returns 面板取单只股票的近期日波动率 (test-v397, Problem 10).

    若无 log_returns 或该 symbol 不在列中，回退到默认日波动率。
    来源: 板块差异化 σ 替代硬编码 0.02。
    """
    if log_returns is not None and symbol in log_returns.columns:
        s = log_returns[symbol].dropna()
        if len(s) >= 20:
            return float(max(s.std(), 0.005))  # 保底 0.5% (防止零波动)
    return _DEFAULT_SIGMA_DAILY

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
        if over.all():  # P1-11 fix: 全超限, clip 到 max_single 后判断可行性
            w = np.full(len(w), max_single)
            total = w.sum()
            if total >= 1.0:
                w = w / total  # 可行: 归一后仍 ≤ max_single
                break
            # 不可行: max_single * n < 1, 无法同时满足 sum=1 和 ≤ max_single
            logger = get_logger("optimizer.portfolio")
            logger.warning(
                "iterative_clip: infeasible constraint max_single=%.4f for %d stocks "
                "(max_single*n=%.4f < 1), returning clipped weights (sum=%.4f < 1)",
                max_single, len(w), total, total
            )
            return w
        w[over] = max_single
        s = w.sum()
        if s <= 0:
            return np.ones(len(w)) / len(w)
        w = w / s
    return w


def _get_regime_max_lots(tier: str, regime_label: str | None) -> int:
    """test-v401: 统一的 tier+regime 手数限制 (Nano/Micro 共享模式).

    Nano: 震荡/熊市→1手, 牛市→不限. 来源: config optimizer.nano.regime_max_lots.
    Micro: 震荡→5手, 熊市→2手, 牛市→不限. 来源: config optimizer.micro.regime_max_lots.
    Small: 不使用 lot cap, 已有 _regime_kelly_fraction() (v397 Problem 7).
    """
    if regime_label is None or regime_label == "unknown":
        return 999
    key = f"optimizer.{tier}.regime_max_lots"
    sizing = _require_cfg(key)
    return int(sizing.get(regime_label, 999))


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
        → 每次进入此分支时读取 config optimizer.risk_aversion (v535)

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
        price_buffer: Optional[float] = None,
        log_returns: Optional[pd.DataFrame] = None,  # v393: 懒计算协方差用
        regime_label: str = None,  # test-v397 (Problem 7): regime-aware Kelly fraction
    ) -> TargetPortfolio:
        """资本自适应组合构建。

        covariance: Small 层 Markowitz 用 (外部预计算或 None)。
        log_returns: 若 covariance=None 且需要协方差, 从 log_returns 懒计算。
          来源: pipeline.py 的 close_df 对数收益面板 (仅 Small 层触发, Nano/Micro 跳过)。
        regime_label: test-v399 — 传入 construct 内部按 tier 分别处理:
          Nano: 限制每只股票手数 (regime_max_lots), 不缩资本 (防 ¥5K 空仓)。
          Micro: 调整分配资本上限 (×regime_sizing)。
          Small: 已有 _regime_kelly_fraction() (v397 Problem 7)。
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
            # test-v399: regime sizing 挪入 construct — Nano 层不缩资本,
            # 改为限制每只股票手数 (震荡/熊市→1手, 牛市→不限)。
            # 原因: v309 在外部缩资 (¥5K×0.6=¥3K) → ¥3K < cheapest_lot → 空仓。
            # ADR-032 反模式 #4: 0 仓位必须暴露, 不得吞掉。
            _max_lots = _get_regime_max_lots("nano", regime_label)
            try:
                result = self._rank_concentrated(a, p, capital, log_returns=log_returns,
                                                  max_lots_per_stock=_max_lots)
            except ValueError as _ve:
                # v399: 恢复 ADR-032 反模式 #4 — 仅 0-lots 错误需特殊日志
                # (其他 ValueError 如 numpy read-only 原样传播)
                if "produced 0 lots" not in str(_ve):
                    raise
                logger.error(
                    "[portfolio] nano tier: capital=¥%s too small for any lot "
                    "(cheapest≈¥%s), re-raising",
                    f"{capital:,.0f}",
                    f"{p.loc[a.index[:min(self.max_positions, len(a))]].min() * LOT_SIZE:,.0f}"
                )
                raise
        elif tier == "micro":
            # test-v401: Micro 层统一为 lot-based regime cap (与 Nano 一致)。
            # v400 用 capital × regime_sizing, 但 fallback 绕过 → 不一致。
            # 新方案: score_weighted 按 alpha 权重分配 raw capital,
            # max_lots_per_stock 限制每只股票手数 (牛=不限, 震荡=5, 熊=2)。
            _max_lots = _get_regime_max_lots("micro", regime_label)
            try:
                result = self._score_weighted_rounding(a, p, capital,
                                                       max_lots_per_stock=_max_lots)
            except ValueError:
                result = TargetPortfolio(pd.Series(dtype=int), capital, "score_weighted", 0.0)
            if result.lots.sum() == 0:
                logger.warning(
                    "[portfolio] micro tier produced 0 lots (capital=¥%s), "
                    "falling back to equal-weight greedy", f"{capital:,.0f}"
                )
                try:
                    result = self._equal_weight_greedy(a, p, capital,
                                                       max_lots_per_stock=_max_lots)
                except ValueError as _eqv:
                    logger.error(
                        "[portfolio] micro tier: even greedy fallback produced 0 lots "
                        "(capital=¥%s, error=%s), re-raising",
                        f"{capital:,.0f}", str(_eqv)
                    )
                    raise
        else:  # small
            # test-v397 (Problem 1): 协方差子集计算 — 仅对 top-K (K=30) 算协方差,
            # 保证 T(252) > K(30), 矩阵良态。全量 800 股票时 N > T 无法求逆。
            if covariance is None and log_returns is not None:
                from quant.risk.covariance import covariance_subset
                _top_syms = list(a.index[:min(self.max_positions, len(a))])
                covariance = covariance_subset(log_returns, _top_syms, method="ledoit_wolf")

            # test-v397 (Problem 11): VaR check now runs here after cov compute
            # (was dead code in pipeline.py Step 4 where cov was always None since v393)
            if covariance is not None:
                try:
                    from quant.risk.var import compute_var
                    _top_subset = a.index[:min(self.max_positions, len(a))]
                    _top_cov = covariance.reindex(index=_top_subset, columns=_top_subset).dropna(how='all').fillna(0)
                    _w_var = pd.Series(1.0 / max(len(_top_cov), 1), index=_top_cov.index)
                    _var_val = compute_var(capital, _w_var, _top_cov, confidence=0.95)
                    if _var_val and abs(_var_val / capital) > 0.03:
                        logger.warning("[portfolio] VaR warning: daily VaR=%.1f (%.1f%% of capital)",
                                       abs(_var_val), abs(_var_val / capital) * 100)
                except Exception as _var_err:
                    logger.debug("[portfolio] VaR check skipped (non-fatal): %s", _var_err)

            result = None
            # 风险优化器分发 (test-v299 §8.2)
            if covariance is not None:
                if self.method == "hrp":
                    rp = self._hrp_lot(a, p, capital, covariance)
                else:
                    rp = self._risk_parity(a, p, capital, covariance)
                if rp.lots.sum() > 0:
                    result = rp
            if result is None:
                if ic_map is not None:
                    result = self._kelly_greedy(a, p, capital, ic_map,
                                                regime_label=regime_label,
                                                covariance=covariance)
                elif covariance is None:
                    raise ValueError(
                        "Mean-variance tier requires covariance matrix. "
                        "Pass covariance= to construct()."
                    )
                else:
                    risk_aversion = float(_require_cfg("optimizer.risk_aversion"))
                    result = self._mean_variance_lot(a, p, capital, covariance, risk_aversion)

        # ── test-v397 (Problem 9): 换手率全局约束 ──
        if current_lots is not None and len(current_lots) > 0:
            result = self._apply_turnover_constraint(result, current_lots, p)
        # ── §8.3 成本带: 拦截不划算的换仓 (Nano层豁免 — 单票集中, 锁仓比换仓更贵) ──
        if current_lots is not None and len(current_lots) > 0 and cost_model is not None \
                and tier != "nano":
            result = self._apply_tc_band(result, current_lots, a, p, cost_model, ic_map,
                                         log_returns=log_returns)
            # test-v397 (Problem 2): TC 过滤后资金再分配 — 被拦截的换仓释放出现金,
            # 按持仓比例重新分配给剩余仓位，避免资金闲置。
            if result.tc_suppressed > 0:
                result = self._rebalance_after_tc(result, p, capital)

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
        log_returns: pd.DataFrame = None,
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
                benefit = float(z[b] - z[a]) * ic_eff * _stock_sigma(b, log_returns) * _TC_HORIZON * swap_val
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
        regime_label: str = None, covariance: Optional[pd.DataFrame] = None,
    ) -> TargetPortfolio:
        """Kelly 头寸分配 (Small 层 ¥100K+ 专用)。

        ⚠️  ADR 032: Kelly 仅在 Small 层启用。Nano/Micro 层禁止。
        在低资本层引入 Kelly 离散化误差 >25%，已反复造成 0 仓位 bug。

        test-v397 (Problem 7): regime-aware — 牛市扩大头寸, 熊市收缩。

        来源: Kelly (1956), Fractional Kelly per Ralph Vince (1990).
        当 ic_map 为 None 或全零时退化为贪心等权 (向后兼容).

        v535: covariance 链路打通 — 原调用不传 cov, compute_kelly_fractions
        恒走默认方差 → 归一化时被消掉 → Kelly 退化为 alpha 比例分配.
        现传 covariance (DataFrame), 逐股 σ² 进入 Kelly 分数 (σ² 维度生效).
        """
        from quant.optimizer.kelly import compute_lot_allocation
        n_stocks = min(self.max_positions, len(alpha))
        if n_stocks == 0:
            return TargetPortfolio(pd.Series(dtype=int), capital, "kelly_greedy", 0.0)
        lots, cash = compute_lot_allocation(
            alpha, prices, capital, ic_map, self.max_positions, LOT_SIZE,
            regime_label=regime_label, cov=covariance,
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

    def _apply_turnover_constraint(
        self, result: TargetPortfolio, current_lots: pd.Series, prices: pd.Series,
    ) -> TargetPortfolio:
        """test-v397 (Problem 9): 全局换手率约束 — 日换手 > max_turnover_ratio 时按权重裁减。

        买卖总量 = Σ|target_lots - current_lots| * lot_value。
        超限时: 保留核心持仓 (差值最大的), 舍去边际最小的, 直到换手在限值内。
        test-v397 fix: diff=0 的持仓同步保留, 防止零换手持仓被误删。
        """
        if _MAX_TURNOVER >= 999:  # 不限制
            return result
        all_syms = result.lots.index.union(current_lots.index)
        tgt = result.lots.reindex(all_syms, fill_value=0).astype(int)
        cur = current_lots.reindex(all_syms, fill_value=0).astype(int)
        diff = tgt - cur

        turnover = 0.0
        for sym in diff.index:
            px = prices.get(sym, 0)
            if px <= 0:
                continue
            turnover += abs(diff[sym]) * px * LOT_SIZE
        capital_est = float((cur * prices.reindex(cur.index, fill_value=0) * LOT_SIZE).sum())
        if capital_est <= 0:
            return result
        ratio = turnover / capital_est
        if ratio <= _MAX_TURNOVER:
            return result

        logger.info(
            f"[portfolio] turnover {ratio:.1%} > {_MAX_TURNOVER:.1%}, "
            f"scaling down by turnover value..."
        )
        # diff=0 的持仓自动保留; diff≠0 的按换手金额排序, 最大的先留
        no_change_syms = set(diff[diff == 0].index)
        trades = []
        for sym in diff.index:
            if diff[sym] == 0:
                continue
            tv = abs(diff[sym]) * prices.get(sym, 0) * LOT_SIZE
            trades.append((sym, diff[sym], tv))
        trades.sort(key=lambda x: -x[2])

        target_tv = _MAX_TURNOVER * capital_est
        kept_tv = 0.0
        kept = set(no_change_syms)
        for sym, d, tv in trades:
            if kept_tv + tv <= target_tv:
                kept.add(sym)
                kept_tv += tv

        trimmed_lots = result.lots.reindex(list(kept), fill_value=0)
        trimmed_lots = trimmed_lots[trimmed_lots > 0]
        trimmed_val = float((trimmed_lots * prices.loc[trimmed_lots.index] * LOT_SIZE).sum()) if len(trimmed_lots) else 0.0
        freed_cash = result.total_value - trimmed_val
        new_cash = result.cash_reserve + freed_cash
        logger.info(f"[portfolio] turnover constrained: {len(result.lots)}→{len(trimmed_lots)} pos")
        return TargetPortfolio(trimmed_lots, round(new_cash, 2), result.method, trimmed_val, result.tc_suppressed)

    def _rebalance_after_tc(
        self, result: TargetPortfolio, prices: pd.Series, capital: float,
    ) -> TargetPortfolio:
        """test-v397 (Problem 2): TC 过滤后现金再分配。

        被成本带拦截的换仓释放出现金, 按持仓权重比例重新分配给剩余仓位,
        避免资金闲置降低收益。
        """
        if result.lots.sum() == 0:
            return result
        current_invested = float((result.lots * prices.loc[result.lots.index] * LOT_SIZE).sum())
        excess = round(result.cash_reserve + result.total_value - current_invested, 2)
        if excess <= 0 or excess < prices.loc[result.lots.index].min() * LOT_SIZE:
            return result  # 不够买一手
        # 按当前仓位权重分配闲置资金
        cur_values = result.lots * prices.loc[result.lots.index] * LOT_SIZE
        weights = cur_values / cur_values.sum()
        new_lots = result.lots.copy()
        remaining = excess
        # 按权重从大到小依次分配 (优先加仓核心持仓)
        for sym in weights.sort_values(ascending=False).index:
            px = prices.get(sym, 0)
            if px <= 0:
                continue
            alloc = excess * weights[sym]
            extra_lots = int(alloc / (px * LOT_SIZE))
            extra_cost = extra_lots * px * LOT_SIZE
            if extra_lots > 0 and extra_cost <= remaining:
                new_lots[sym] += extra_lots
                remaining -= extra_cost
        new_invested = float((new_lots * prices.loc[new_lots.index] * LOT_SIZE).sum())
        new_cash = round(remaining, 2)
        logger.info(f"[portfolio] post-TC rebalance: excess=¥{excess:,.0f} → "
                    f"invested=¥{new_invested:,.0f} cash=¥{new_cash:,.0f}")
        return TargetPortfolio(new_lots, new_cash, result.method, new_invested, result.tc_suppressed)

    def _rank_concentrated(
        self, alpha: pd.Series, prices: pd.Series, capital: float,
        log_returns: Optional[pd.DataFrame] = None,
        max_lots_per_stock: int = 999,
    ) -> TargetPortfolio:
        """排名集中: 按 alpha 降序逐只满仓买入, 直至资金不足买下一手。
        v393: 2+持仓时用协方差剔除高相关性票 (ρ>0.7则弃低alpha).
        v399: max_lots_per_stock — regime 手数限制 (牛=不限, 震荡/熊=1手)。
        """
        n_candidates = min(self.max_positions, len(alpha))
        if n_candidates == 0:
            return TargetPortfolio(pd.Series(dtype=int), capital, "rank_concentrated", 0.0)

        cheapest_lot = prices.loc[alpha.index[:n_candidates]].min() * LOT_SIZE

        lots = pd.Series(0, index=alpha.index, dtype=int)
        cash = capital
        symbol_order = list(alpha.index[:n_candidates])

        for sym in symbol_order:
            cost_per_lot = prices[sym] * LOT_SIZE
            if cash < cost_per_lot:
                continue
            # v399: regime 手数限制 — 每只股票最多 max_lots_per_stock 手
            max_lots = min(int(cash // cost_per_lot), max_lots_per_stock)
            lots[sym] = max_lots
            cash -= max_lots * cost_per_lot
            if cash < cheapest_lot:
                break

        # v393: 2+持仓→算协方差→剔除高相关低alpha票
        selected = lots[lots > 0]
        if len(selected) >= 2 and log_returns is not None:
            from quant.risk.covariance import covariance_matrix
            _syms = list(selected.index)
            _common = [s for s in _syms if s in log_returns.columns]
            if len(_common) >= 2:
                cov_sub = covariance_matrix(log_returns[_common], method="ledoit_wolf")
                std = np.sqrt(np.diag(cov_sub.values))
                # v399: 显式构造 DataFrame 避 numpy read-only 错误
                # corr.values[:] = ... 在某些 numpy 版本 .values 返回只读视图
                _corr_vals = cov_sub.values / np.outer(std, std)
                corr = pd.DataFrame(_corr_vals, index=cov_sub.index, columns=cov_sub.columns)
                # 剔除高相关低alpha票 (ρ > 0.7, 来源: WorldQuant 冗余阈值)
                dropped = set()
                for i, s1 in enumerate(_common):
                    for s2 in _common[i+1:]:
                        if abs(corr.loc[s1, s2]) > 0.7:
                            loser = s1 if alpha.get(s1, 0) < alpha.get(s2, 0) else s2
                            dropped.add(loser)
                if dropped:
                    lots = lots.drop(list(dropped), errors='ignore')
                    logger.info("[rank_concentrated] dropped %d correlated: %s", len(dropped), sorted(dropped))

        selected = lots[lots > 0]
        total_value = (selected * prices * LOT_SIZE).sum()
        if selected.sum() == 0:
            raise ValueError(
                f"rank_concentrated produced 0 lots: "
                f"n_candidates={n_candidates} capital={capital:,.0f} "
                f"cheapest_lot={cheapest_lot:,.0f}"
            )
        return TargetPortfolio(selected, round(capital - total_value, 2), "rank_concentrated", total_value)

    def _equal_weight_greedy(
        self, alpha: pd.Series, prices: pd.Series, capital: float,
        max_lots_per_stock: int = 999,
    ) -> TargetPortfolio:
        """贪心等权: 每轮给得分最高的未满仓股票加 1 手。
        v401: max_lots_per_stock — regime 手数限制 (牛=不限, 震荡/熊=有限制)。
        """
        n_stocks = min(self.max_positions, len(alpha))
        if n_stocks == 0:
            return TargetPortfolio(pd.Series(dtype=int), capital, "equal_weight", 0.0)
        lots = pd.Series(0, index=alpha.index, dtype=int)
        cash = capital
        symbol_order = list(alpha.index[:n_stocks])
        max_lots_per = max(1, int(capital / (n_stocks * prices.loc[alpha.index[:n_stocks]].mean() * LOT_SIZE)) + 1)
        max_lots_per = min(max_lots_per, max_lots_per_stock)  # v401: regime cap
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
        max_lots_per_stock: int = 999,
    ) -> TargetPortfolio:
        """得分倾斜 + 整数舍入。
        v401: max_lots_per_stock — regime 手数限制 (牛=不限, 震荡/熊=有限制)。
        """
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
            n_lots = min(n_lots, max_lots_per_stock)  # v401: regime cap
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
        参数 risk_aversion 来自 config optimizer.risk_aversion (v535:
        原 calibrate_risk_aversion 网格目标函数单调, 恒选左边界 0.5, "自适应"为假).
        B4 (2026-08-18): λ 经 ridge 正则化进入协方差 (Σ + λ·τ·I) —
        原 w = inv(Σ)@α/λ 归一化后 λ 抵消.
        """
        n_stocks = min(self.max_positions, len(alpha))
        top = alpha.iloc[:n_stocks]
        p = prices.loc[top.index]
        if covariance is not None:
            common_cov = [s for s in top.index if s in covariance.index]
            if len(common_cov) >= 3:
                alpha_vec = top.loc[common_cov].values
                Sigma = covariance.loc[common_cov, common_cov].values
                _tau = float(np.trace(Sigma) / len(Sigma)) if len(Sigma) else 1.0
                _Sig_reg = Sigma + risk_aversion * _tau * np.eye(len(Sigma))
                try:
                    inv_Sigma = np.linalg.inv(_Sig_reg)
                except np.linalg.LinAlgError:
                    inv_Sigma = np.linalg.pinv(_Sig_reg)
                    logger.warning("[mean_variance_lot] near-singular covariance, using pseudo-inverse")
                w_raw = inv_Sigma @ alpha_vec
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
