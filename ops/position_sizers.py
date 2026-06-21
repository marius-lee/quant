"""仓位算法 — 4 种独立仓位计算公式 + 启动先验.
每个 sizer 接收 (capital, win_rate, avg_win, avg_loss, signal) → lots.
来源: Kelly 1956, Chan 第6章, Wilson 1927, Ryan Jones The Trading Game
"""
import math

# ═══ 启动先验 (Bayesian pseudo-counts) ═══
# 来源: Grinold "良好策略"基准(IR=0.5→胜率~55%), Chan 示例(b=1.5)
PRIOR_WINS = 5.5       # 55% × 10 笔伪交易
PRIOR_LOSSES = 4.5
PRIOR_AVG_WIN = 200.0  # ¥200 (~4% of ¥5,000)
PRIOR_AVG_LOSS = 133.0 # ¥200 / 1.5

# ═══ 通用参数 ═══
FIXED_RATIO_DELTA = 500  # 每增1手需¥500累计盈利 (来源: Ryan Jones 默认, 保守)
MIN_LOTS = 1
SHARES_PER_LOT = 100


def _effective_stats(strategy_tc, strategy_name: str) -> tuple:
    """合并先验 + 真实交易统计."""
    import sqlite3
    rows = strategy_tc.execute(
        "SELECT pnl FROM sim_trades WHERE side='sell' AND strategy=? AND pnl IS NOT NULL",
        (strategy_name,)).fetchall()
    pnls = [r[0] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # 贝叶斯合并: 先验伪计数 + 真实计数
    n_wins = PRIOR_WINS + len(wins)
    n_losses = PRIOR_LOSSES + len(losses)
    n = n_wins + n_losses
    p = n_wins / n if n > 0 else 0.55

    # 平均盈利/亏损 (加权: 先验均值×先验权重 + 真实均值×真实权重)
    if wins:
        real_avg_win = sum(wins) / len(wins)
    else:
        real_avg_win = PRIOR_AVG_WIN
    if losses:
        real_avg_loss = abs(sum(losses) / len(losses))
    else:
        real_avg_loss = PRIOR_AVG_LOSS

    # 权重按样本量比例
    real_weight = min(len(pnls) / 50.0, 0.83)  # 50笔后真实权重≥83%
    prior_weight = 1.0 - real_weight
    avg_win = prior_weight * PRIOR_AVG_WIN + real_weight * real_avg_win
    avg_loss = prior_weight * PRIOR_AVG_LOSS + real_weight * real_avg_loss

    b = avg_win / avg_loss if avg_loss > 0 else 1.5
    return p, b, avg_win, avg_loss, n_wins + n_losses


def wilson_lower_bound(wins: float, n: float, z: float = 1.96) -> float:
    """胜率 95% 置信下界. 来源: Wilson 1927"""
    if n < 1:
        return 0.0
    p = wins / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, center - margin)


def compute_lots_full_kelly(strategy_tc, capital: float,
                             entry_price: float) -> int:
    """全 Kelly: f* = (bp-q)/b. 来源: Kelly 1956"""
    p, b, _, _, _ = _effective_stats(strategy_tc, "chen_fullkelly")
    if b <= 0 or p <= 0:
        return 0
    f = max(0.0, (b * p - (1 - p)) / b)
    risk_capital = capital * f
    lots = int(risk_capital / (entry_price * SHARES_PER_LOT + 5))
    return max(0, min(lots, int(capital / (entry_price * SHARES_PER_LOT + 5))))


def compute_lots_half_kelly(strategy_tc, capital: float,
                             entry_price: float) -> int:
    """半 Kelly: f*/2. 来源: Chan 第6章"""
    p, b, _, _, _ = _effective_stats(strategy_tc, "chen_halfkelly")
    if b <= 0 or p <= 0:
        return 0
    f = max(0.0, (b * p - (1 - p)) / b) / 2.0
    risk_capital = capital * f
    lots = int(risk_capital / (entry_price * SHARES_PER_LOT + 5))
    return max(0, min(lots, int(capital / (entry_price * SHARES_PER_LOT + 5))))


def compute_lots_wilson(strategy_tc, capital: float,
                         entry_price: float) -> int:
    """威尔逊 95%CI 下界 → 半 Kelly. 来源: Wilson 1927 + Chan"""
    _, _, _, _, n_eff = _effective_stats(strategy_tc, "chen_wilson")
    p_raw, b, _, _, _ = _effective_stats(strategy_tc, "chen_wilson")
    p_lower = wilson_lower_bound(p_raw * n_eff, n_eff)
    if b <= 0 or p_lower <= 0:
        return 0
    f = max(0.0, (b * p_lower - (1 - p_lower)) / b) / 2.0
    risk_capital = capital * f
    lots = int(risk_capital / (entry_price * SHARES_PER_LOT + 5))
    return max(0, min(lots, int(capital / (entry_price * SHARES_PER_LOT + 5))))


def compute_lots_fixed_ratio(strategy_tc, capital: float,
                              entry_price: float,
                              delta: float = FIXED_RATIO_DELTA) -> int:
    """固定比率: N = ½×√(8P/δ+1)+1. 来源: Ryan Jones"""
    rows = strategy_tc.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM sim_trades WHERE side='sell' AND strategy='chen_fixedratio' AND pnl IS NOT NULL"
    ).fetchone()
    P = max(0.0, rows[0]) if rows else 0.0
    N = max(1, int(0.5 * math.sqrt(8 * P / delta + 1) + 1))
    max_affordable = int(capital / (entry_price * SHARES_PER_LOT + 5))
    return min(N, max_affordable)
