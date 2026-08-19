"""Kelly 公式头寸管理 — 替代等权分配，按因子预期收益分配仓位。

⚠️  ADR 032: 此模块仅在 Small 层 (≥¥100K) 的 construct() 中被调用。
Nano/Micro 层不使用 Kelly — 整数手离散化误差压倒优化收益。
严禁在低资本层引入 Kelly，这是已验证的反复出现的错误。

理论来源: Kelly (1956) "A New Interpretation of Information Rate".
Fractional Kelly: 使用 1/N 凯利降低波动 (Ralph Vince 1990).

集成点: 在 optimizer/portfolio.py 的 _equal_weight_greedy 之前调用，
用 Kelly 分数替代等权分配。

公式:
  Kelly 比例 = (μ - r_f) / σ²
  μ  = 因子预期收益 (IC × 日波动率中位数)
  σ² = 因子收益方差
  Fractional Kelly: f* = Kelly / k (k=4 为四分之一凯利, 保守)

test-v397 (Problem 7): regime-aware Kelly — 牛/熊/震荡动态调整 kelly_fraction。
  牛市 (bull): 0.8 — 扩大头寸
  震荡 (sideways): 0.5 — 中性
  熊市 (bear): 0.2 — 收缩头寸, 防回撤
  来源: regime.kelly.{bull,sideways,bear} in config.yaml.
"""

import numpy as np
import pandas as pd
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

_log = get_logger("optimizer.kelly")


def _regime_kelly_fraction(regime_label: str = None) -> float:
    """test-v397: 根据 regime 返回动态 Kelly fraction.

    None / unknown → 回退到 optimizer.kelly_fraction (默认 0.5).
    """
    if not regime_label or regime_label == "unknown":
        return float(_require_cfg("optimizer.kelly_fraction"))
    try:
        return float(_require_cfg(f"regime.kelly.{regime_label}"))
    except (KeyError, TypeError):
        _log.warning("kelly: unknown regime_label=%s, using default", regime_label)
        return float(_require_cfg("optimizer.kelly_fraction"))


def compute_kelly_fractions(
    alpha: pd.Series,
    ic_map: dict = None,
    fraction: float = None,
    cov: np.ndarray = None,
    regime_label: str = None,
) -> pd.Series:
    """计算每只候选股的 Kelly 分数.

    Args:
        alpha: 股票得分 Series (index=symbol, value=score)
        ic_map: {factor_name: ic_value} — 各因子的 IC 值, 用于估算预期收益.
                如果为 None 或空, 按 alpha 得分比例分配 (退化到等权).
        fraction: Fractional Kelly 分数, 默认从 config 读 (optimizer.kelly_fraction).
        regime_label: test-v397: 市场状态 (bull/sideways/bear), 动态调 kelly_fraction.

    Returns:
        Series (index=symbol, value=Kelly fraction) — 总和 ≤ 1.
    """
    if fraction is None:
        fraction = _regime_kelly_fraction(regime_label)

    # 展平嵌套 ic_map (兼容 compute_ic 产出格式)
    if ic_map and isinstance(next(iter(ic_map.values())), dict):
        ic_map = {k: v.get("ic_ir", 0) for k, v in ic_map.items()}
        _log.debug("Flattened nested ic_map -> %d factors", len(ic_map))

    if ic_map is None or not ic_map:
        # 无 IC 信息 → 退化为 alpha 比例分配
        _log.debug("No IC map — falling back to alpha-proportional allocation")
        kelly = _alpha_proportional(alpha)
    else:
        # ── 从 IC 估算预期收益 ──
        # 每个股票的预期收益 = 各因子 IC 加权 × 股票在该因子的得分
        # 简化: 用 med_IC 作为全局预期收益参数
        ic_values = np.array(list(ic_map.values()))
        med_ic = np.median(np.abs(ic_values))

        # IC 退化保护: 所有 IC=0 → 退化为 alpha 比例
        if med_ic < 1e-6:
            _log.debug("All IC ≈ 0 — falling back to alpha-proportional")
            kelly = _alpha_proportional(alpha)
        else:
            # 预期收益 μ = med_IC × 日波动率代理
            # 使用 alpha 得分的标准化值作为 μ 的代理
            mu = alpha / alpha.abs().max() * med_ic if alpha.abs().max() > 0 else alpha

            # σ²: A股日收益率典型方差 ≈ 0.0004 (σ_daily ≈ 2%)
            # 来源: CSRC 2025年度报告 + 2026-07-21 audit C5
            # alpha.var() 是截面方差(~1.0), 非收益率方差, 会导致 Kelly ~0
            # B28: default return variance (σ≈2%), should be overridden by covariance matrix
            # ALG5: dynamic return variance — prioritize covariance diag, fallback to default.
            # Default 0.0004 = σ_daily≈2% per CSRC 2025 report.
            DEFAULT_RETURN_VAR = 0.0004
            # B16 (2026-08-18): 原取 mean(cov_diag) 单一标量 → kelly_raw 归一化时
            # 被消掉 → Kelly 退化为 alpha 比例 (σ² 维度信息丢失). 改用逐股方差
            # 向量 (cov 对角), 每只股票按自身波动率缩放.
            # v535: cov 链路打通 — 此前 _kelly_greedy 从未传 cov (portfolio.py),
            # cov=None → var=常数 → 归一化时被消掉, "自适应"实为 alpha 比例分配.
            # 现支持 DataFrame (portfolio 传 covariance) 与 ndarray 两态,
            # DataFrame 按 alpha.index 对齐 (与 compute_lot_allocation 同序).
            var = DEFAULT_RETURN_VAR
            if cov is not None:
                if isinstance(cov, pd.DataFrame):
                    _cov = cov.reindex(index=alpha.index, columns=alpha.index)
                    cov_diag = np.nan_to_num(_cov.values.diagonal(), nan=DEFAULT_RETURN_VAR)
                else:
                    cov_diag = np.diag(cov) if isinstance(cov, np.ndarray) else np.array(cov).diagonal()
                if len(cov_diag) > 0 and np.nanmean(cov_diag) > 1e-8:
                    var = np.asarray(cov_diag, dtype=float)
                    var = np.where(np.isnan(var) | (var <= 1e-8), DEFAULT_RETURN_VAR, var)
                    _log.debug("kelly: per-stock var from cov diag (mean=%.6f)", var.mean())

            # Kelly: f = (μ - r_f) / σ², r_f=0 (A股无风险利率极低)
            # 过滤负 Kelly (因子预期该股下跌)
            kelly_raw = (mu / np.maximum(var, 1e-8)).clip(lower=0)

            # ── 归一化: 相对比例总和 = 1 (必须在 ×fraction 之前完成) ──
            total = kelly_raw.sum()
            if total <= 0:
                kelly = _alpha_proportional(alpha)
            else:
                kelly = kelly_raw / total

    # ── Fractional Kelly: 按 fraction 缩放部署资本 (regime-aware) ──
    # CODE-REVIEW P0-10 fix: 原代码 kelly_raw/fraction 语义反了, 且归一化
    # (除以 sum) 与乘/除 fraction 数学上相互抵消 → 熊市 fraction=0.2 缩仓空操作。
    # 正确顺序: 归一化(sum=1) → ×fraction(总仓位强度) → clip(max_single)。
    # B16: ic_map 缺失 / IC=0 退化路径原直接 return _alpha_proportional —
    # 跳过 fraction 与 max_single clip → 熊市满仓. 所有路径统一走公共缩放.
    kelly = kelly * fraction

    # 单票仓位上限 (risk.max_single_position)。
    # clip 后总和可略低于 fraction — 保守方向, 可接受, 不再重归一化 (v406)。
    max_single = _require_cfg("risk.max_single_position")
    kelly = kelly.clip(upper=max_single)

    _log.debug(
        f"Kelly fractions: {len(kelly)} stocks, "
        f"top3={kelly.iloc[:3].round(3).to_dict() if len(kelly) >= 3 else kelly.round(3).to_dict()}"
    )
    return kelly


def _alpha_proportional(alpha: pd.Series) -> pd.Series:
    """退化为 alpha 得分比例分配 (等权的变体).
    v554 (P1-4): alpha 含负值时 clip(0) — 原 alpha/alpha.sum() 直接归一,
    负权重压低 Σw (A股不能做空, 实测仅 ~15% 资金部署)."""
    a = alpha.clip(lower=0)
    if a.sum() == 0:
        return pd.Series(1.0 / max(len(a), 1), index=alpha.index)
    return a / a.sum()


def compute_lot_allocation(
    alpha: pd.Series,
    prices: pd.Series,
    capital: float,
    ic_map: dict = None,
    max_positions: int = None,
    lot_size: int = 100,
    regime_label: str = None,
    cov: np.ndarray | pd.DataFrame = None,
) -> tuple[pd.Series, float]:
    """用 Kelly 分数计算整数手分配.

    Args:
        alpha: 股票得分 (index=symbol, value=score)
        prices: 股价 (index=symbol, value=price)
        capital: 可用资金
        ic_map: 因子 IC 映射
        max_positions: 最大持仓数
        lot_size: 每手股数 (A股=100)
        regime_label: test-v397: market regime for dynamic kelly fraction
        cov: v535 — 协方差矩阵 (DataFrame index=symbol 或 ndarray, 与
             top_alpha 对齐), 提供逐股日收益率方差 (σ² 维度, 打破
             "Kelly=alpha 比例" 退化). None=默认方差 0.0004.

    Returns:
        (lots Series, remaining_cash)
    """
    if max_positions is None:
        max_positions = _require_cfg("risk.max_positions")

    n = min(max_positions, len(alpha))
    top_alpha = alpha.iloc[:n]
    top_prices = prices.loc[top_alpha.index]

    kelly_weights = compute_kelly_fractions(top_alpha, ic_map, regime_label=regime_label,
                                            cov=cov)
    kelly_weights = kelly_weights.loc[top_alpha.index].fillna(0)
    if kelly_weights.sum() == 0:
        kelly_weights = pd.Series(1.0 / n, index=top_alpha.index)

    lots = pd.Series(0, index=top_alpha.index, dtype=int)
    cash = capital

    # 按 Kelly 权重分配资金
    for sym in top_alpha.index:
        alloc = capital * kelly_weights.get(sym, 0)
        n_lots = int(alloc / (top_prices[sym] * lot_size))
        if n_lots > 0:
            cost = n_lots * top_prices[sym] * lot_size
            if cost <= cash:
                lots[sym] = n_lots
                cash -= cost

    # v554 (P1-2): 整手截断残差回收 — int(alloc/手) 系统性截断, 剩余现金
    # 贪心补 1 手 (手成本低者优先), 直至现金不足或全部达上限.
    # 原实现仅处理 lots.sum()==0 极端情况, 常态闲置不回收.
    # v555 (E3): 回收加 max_single 集中度守卫 — 与 portfolio._recycle_residual_cash
    # 一致; 原无守卫时补 1 手可把单票市值推至资本 2× 上限 (Small 层实测风险)
    if cash > 0:
        _cap = float(_require_cfg("risk.max_single_position"))
        for sym in sorted(top_alpha.index, key=lambda s: top_prices[s] * lot_size):
            cost = top_prices[sym] * lot_size
            if (lots[sym] + 1) * cost > _cap * capital:
                continue
            if cash >= cost:
                lots[sym] += 1
                cash -= cost

    return lots[lots > 0], round(cash, 2)
