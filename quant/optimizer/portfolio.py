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
_DEFAULT_SIGMA_DAILY = _require_cfg("execution.default_daily_vol")  # 典型日波动率 (来源: config.yaml execution.default_daily_vol)
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
    """迭代裁剪+重归一化: 所有权重 ≤ max_single; Σ=1 在可行时达成。
    算法: 反复裁剪超限权重, 剩余分配给未超限的。超限数单调递减, 保证收敛。
    来源: 2026-07-21 audit H6; De Prado & Lewis (2019) Ch.3.
    v554 (P1): 重写 — 原"裁剪→整体归一"在超限集上振荡
    (裁 A→归 B 超→裁 B→归 A 超), max_iter 内可能不收敛:
      1. n×max_single ≥ 1 但归一后每只 > max_single 时, 静默返回超限权重 6 次
      2. 不可行 (n×max_single<1) 返回 Σ<1 权重 (25-75% 资金闲置) 仅 warning
    新算法: 裁剪后 Σ≥1 → 等比缩放 (缩小不制造超限, 一次收敛);
    Σ<1 → 剩余容量只分配给未超限者 (超限集单调扩张 ≤ n 轮收敛);
    不可行 → 等权最大 deploy + warning.
    v555 (E2): 稀疏正权输入 (MV 负权置 0) 且全部正权股已到限时,
    剩余 gap 无处分配 (0 权股无 alpha 信号, 强买违背优化意图) —
    合法保留现金但必须显式告警, 禁止静默返回 Σ<1.
    """
    import numpy as np
    w = np.asarray(w, dtype=float).copy()
    w = np.clip(w, 0.0, max_single)
    if w.sum() >= 1.0:
        return w / w.sum()  # 等比缩放 ≤ max_single (系数 ≤ 1), 一次收敛
    for _ in range(max_iter):
        over = w >= max_single - 1e-12
        if over.all():
            break
        gap = 1.0 - w[over].sum()
        free = ~over
        fs = w[free].sum()
        if fs > 0:
            w[free] = w[free] / fs * gap
        else:
            # v555 (E2): free 集全 0 = 所有正权股均已到限, gap 无处可分配.
            # 稀疏正权 (MV) 的合法状态 — 保留现金, 显式告警不静默.
            logger = get_logger("optimizer.portfolio")
            logger.warning(
                "iterative_clip: all positive-weight names at max_single, "
                "gap=%.4f unallocatable (sparse weights), returning sum=%.4f < 1",
                gap, w.sum(),
            )
            break
        w[free] = np.minimum(w[free], max_single)
    if len(w) * max_single < 1.0:
        logger = get_logger("optimizer.portfolio")
        logger.warning(
            "iterative_clip: infeasible constraint max_single=%.4f for %d stocks "
            "(max_single*n=%.4f < 1), returning clipped weights (sum=%.4f < 1)",
            max_single, len(w), len(w) * max_single, w.sum()
        )
        return np.full(len(w), max_single)  # 不可行下最大 deploy (Σ=n*max_single<1)
    return w
def _get_regime_max_lots(tier: str, regime_label: str | None) -> int:
    """test-v401: 统一的 tier+regime 手数限制 (Nano/Micro 共享模式).
    Nano: 不限手数 (v594: 集中买入排名靠前股票).
    Micro: 不限手数 (v594: 集中买入排名靠前股票).
    Small: 不使用 lot cap, 已有 _regime_kelly_fraction() (v397 Problem 7).
    """
    if regime_label is None or regime_label == "unknown":
        return 999
    key = f"optimizer.{tier}.regime_max_lots"
    sizing = _require_cfg(key)
    return int(sizing.get(regime_label, 999))
