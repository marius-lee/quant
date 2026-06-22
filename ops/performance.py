"""Grinold & Kahn IC/IR/BR metrics for all 4 strategies.
来源: 《主动投资组合管理》第6/10/12/17章
IR = IC × √BR (主动管理基本定律)
Alpha = Volatility × IC × Score (预测基本公式)

用法:
    PYTHONPATH=. python3 ops/performance.py            # 所有策略
    PYTHONPATH=. python3 ops/performance.py chen       # 单个策略
    PYTHONPATH=. python3 ops/performance.py chen --force  # 强制重算
"""
import sqlite3, os, math, sys, json
from datetime import date, datetime, timedelta
from collections import defaultdict

TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")
MARKET_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")


def _ensure_table(tc: sqlite3.Connection) -> None:
    tc.execute("""CREATE TABLE IF NOT EXISTS strategy_metrics (
        strategy         TEXT PRIMARY KEY,
        ic_pearson_1d    REAL,
        ic_pearson_3d    REAL,
        ic_pearson_5d    REAL,
        ic_pearson_20d   REAL,
        ic_spearman_1d   REAL,
        ic_spearman_3d   REAL,
        ic_spearman_5d   REAL,
        ic_spearman_20d  REAL,
        ir_annualized    REAL,
        br_bets_per_year REAL,
        ir_implied       REAL,
        n_signals        INTEGER,
        n_trades         INTEGER,
        data_quality     TEXT,
        computed_at      TEXT
    )""")
    tc.commit()


def _pearson(xs: list, ys: list) -> float | None:
    """Pearson相关系数. n<5或零方差→None. 来源: 附录C"""
    n = len(xs)
    if n < 5:
        return None
    sx = sum(xs); sy = sum(ys)
    sxx = sum(x*x for x in xs); syy = sum(y*y for y in ys)
    sxy = sum(x*y for x, y in zip(xs, ys))
    denom = math.sqrt((n*sxx - sx*sx) * (n*syy - sy*sy))
    if denom == 0:
        return None
    return round((n*sxy - sx*sy) / denom, 4)


def _rank_transform(values: list) -> list:
    """值→秩(ties取平均). 最小值秩=1. 来源: Spearman秩相关标准实现"""
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j - 1) / 2.0 + 1  # 1-based
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def _spearman(xs: list, ys: list) -> float | None:
    """Spearman秩相关系数. 来源: Grinold第12章"""
    return _pearson(_rank_transform(xs), _rank_transform(ys))


def _forward_returns_batch(mc: sqlite3.Connection, signal_rows: list,
                           horizon: int) -> list:
    """批量计算远期收益. signal_rows: [(date, symbol, score), ...].
    使用LEAD窗口函数(需要SQLite≥3.25). 返回: [(date, score, fwd_ret), ...]"""
    if not signal_rows:
        return []
    results = []
    for sig_date, sym, score in signal_rows:
        row = mc.execute("""
            WITH ranked AS (
                SELECT date, close,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date) -
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date) as grp
                FROM daily WHERE symbol=?
            )
            SELECT close FROM daily WHERE symbol=? AND date >= ?
            ORDER BY date LIMIT 1 OFFSET ?
        """, (sym, sym, sig_date, horizon)).fetchone()
        # Simpler approach: just query directly
        fwd_date = mc.execute("""
            SELECT date FROM daily WHERE symbol=? AND date > ?
            ORDER BY date LIMIT 1 OFFSET ?
        """, (sym, sig_date, horizon - 1)).fetchone()
        if not fwd_date:
            continue
        fwd = mc.execute(
            "SELECT close FROM daily WHERE symbol=? AND date=?",
            (sym, fwd_date[0])
        ).fetchone()
        base = mc.execute(
            "SELECT close FROM daily WHERE symbol=? AND date=?",
            (sym, sig_date)
        ).fetchone()
        if not fwd or not base or fwd[0] <= 0 or base[0] <= 0:
            continue
        ret = fwd[0] / base[0] - 1
        if -0.99 <= ret <= 5.0:  # 过滤极端值(拆股/数据错误)
            results.append((sig_date, score, round(ret, 4)))
    return results


def _compute_chen_ic(tc: sqlite3.Connection,
                     mc: sqlite3.Connection) -> dict:
    """陈小群策略横截面IC. 对每个≥5信号的日期计算Pearson/Spearman IC.
    尝试多时间窗口: 1d/3d/5d/20d, 根据可用远期数据自动裁剪."""
    rows = tc.execute(
        "SELECT DISTINCT date FROM signals ORDER BY date"
    ).fetchall()
    dates = [r[0] for r in rows]

    # 检查远期数据可用性
    max_daily = mc.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    max_signal = max(dates) if dates else ""

    # 统计: {horizon: [ic_values]}
    ic_pearson = defaultdict(list)
    ic_spearman = defaultdict(list)
    total_signals = 0; valid_dates = 0

    for d in dates:
        signals = tc.execute(
            "SELECT symbol, score FROM signals WHERE date=? AND score IS NOT NULL",
            (d,)
        ).fetchall()
        if len(signals) < 5:
            continue
        total_signals += len(signals)

        for horizon in [1, 3, 5, 20]:
            fwd = _forward_returns_batch(mc, [(d, s[0], s[1]) for s in signals], horizon)
            if len(fwd) >= 5:
                scores = [r[1] for r in fwd]
                rets = [r[2] for r in fwd]
                p = _pearson(scores, rets)
                s = _spearman(scores, rets)
                if p is not None: ic_pearson[horizon].append(p)
                if s is not None: ic_spearman[horizon].append(s)

        valid_dates += 1

    def _mean(vals): return round(sum(vals)/len(vals), 4) if vals else None
    return {
        "ic_pearson_1d": _mean(ic_pearson.get(1, [])),
        "ic_pearson_3d": _mean(ic_pearson.get(3, [])),
        "ic_pearson_5d": _mean(ic_pearson.get(5, [])),
        "ic_pearson_20d": _mean(ic_pearson.get(20, [])),
        "ic_spearman_1d": _mean(ic_spearman.get(1, [])),
        "ic_spearman_3d": _mean(ic_spearman.get(3, [])),
        "ic_spearman_5d": _mean(ic_spearman.get(5, [])),
        "ic_spearman_20d": _mean(ic_spearman.get(20, [])),
        "n_signals": total_signals,
        "n_dates": valid_dates,
        "max_forward_date": max_daily,
        "max_signal_date": max_signal,
    }


def _compute_ir(strategy: str, tc: sqlite3.Connection) -> tuple:
    """IR = mean(PnL%)/std(PnL%) × sqrt(年化因子). 来源: Grinold 5.4节"""
    rows = tc.execute(
        "SELECT date, pnl FROM sim_trades WHERE side='sell' AND strategy=? AND pnl IS NOT NULL ORDER BY date",
        (strategy,)
    ).fetchall()
    if len(rows) < 3:
        return None, len(rows)

    from config.loader import get as cfg
    base = float(cfg("backtest.initial_capital", 5000))
    rets = [r[1] / base for r in rows]
    n = len(rets)
    mean_r = sum(rets) / n
    var_r = sum((r - mean_r)**2 for r in rets) / (n - 1) if n > 1 else 0
    if var_r <= 0:
        return None, n

    std_r = math.sqrt(var_r)
    # 年化: 计算实际时间跨度
    d1 = datetime.strptime(rows[0][0], "%Y-%m-%d")
    d2 = datetime.strptime(rows[-1][0], "%Y-%m-%d")
    years = max((d2 - d1).days / 365.0, 1/365)
    annual_factor = math.sqrt(n / years) if years > 0 else 1
    ir = round(mean_r / std_r * math.sqrt(1.0 / max(years, 1/365)), 4)
    return ir, n


def _compute_br(strategy: str, tc: sqlite3.Connection) -> tuple:
    """BR = n_buys / years_of_data. 来源: Grinold 6.2节"""
    dates = tc.execute(
        "SELECT MIN(date), MAX(date) FROM sim_trades WHERE strategy=?",
        (strategy,)
    ).fetchone()
    if not dates or not dates[0]:
        return None, 0

    n_buys = tc.execute(
        "SELECT COUNT(*) FROM sim_trades WHERE strategy=? AND side='buy'",
        (strategy,)
    ).fetchone()[0]

    d1 = datetime.strptime(dates[0], "%Y-%m-%d")
    d2 = datetime.strptime(dates[1], "%Y-%m-%d")
    years = max((d2 - d1).days / 365.0, 1/365)
    br = round(n_buys / years, 2)
    return br, n_buys


def compute_strategy_metrics(strategy: str = "chen",
                              force: bool = False) -> dict:
    """计算或返回缓存的IC/IR/BR."""
    tc = sqlite3.connect(TRADE_DB)
    mc = sqlite3.connect(MARKET_DB)
    _ensure_table(tc)

    # 缓存检查
    if not force:
        cached = tc.execute(
            "SELECT computed_at FROM strategy_metrics WHERE strategy=?",
            (strategy,)
        ).fetchone()
        if cached and cached[0]:
            try:
                cached_time = datetime.fromisoformat(cached[0])
                age_h = (datetime.now() - cached_time).total_seconds() / 3600
                newest_trade = tc.execute(
                    "SELECT MAX(date) FROM sim_trades WHERE strategy=?",
                    (strategy,)
                ).fetchone()[0]
                if age_h < 24 and newest_trade and newest_trade <= cached[0][:10]:
                    row = tc.execute(
                        "SELECT * FROM strategy_metrics WHERE strategy=?",
                        (strategy,)
                    ).fetchone()
                    cols = [d[1] for d in tc.execute(
                        "PRAGMA table_info(strategy_metrics)").fetchall()]
                    tc.close(); mc.close()
                    return dict(zip(cols, row))
            except Exception:
                pass  # 缓存损坏, 重算

    # 计算IC
    if strategy == "chen":
        ic = _compute_chen_ic(tc, mc)
    else:
        ic = {"ic_pearson_1d": None, "ic_pearson_3d": None,
              "ic_pearson_5d": None, "ic_pearson_20d": None,
              "ic_spearman_1d": None, "ic_spearman_3d": None,
              "ic_spearman_5d": None, "ic_spearman_20d": None,
              "n_signals": 0, "n_dates": 0}

    # 计算IR/BR
    ir_val, n_trades = _compute_ir(strategy, tc)
    br_val, n_buys = _compute_br(strategy, tc)

    # 基本定律交叉验证: IR ≈ IC_mean × √BR (取最近可用IC)
    ic_for_ir = ic.get("ic_pearson_1d") or ic.get("ic_pearson_3d") or ic.get("ic_pearson_5d")
    ir_implied = round(ic_for_ir * math.sqrt(br_val), 4) \
        if ic_for_ir and br_val and br_val > 0 else None

    # 数据质量评估 (来源: Grinold IR标准误≈1/√(2T), 表17-1)
    n_dates = ic.get("n_dates", 0)
    n_sig = ic.get("n_signals", 0)
    years_of_history = 0.0
    try:
        max_d = tc.execute("SELECT MAX(date) FROM sim_trades WHERE strategy=?",
                          (strategy,)).fetchone()[0]
        min_d = tc.execute("SELECT MIN(date) FROM sim_trades WHERE strategy=?",
                          (strategy,)).fetchone()[0]
        if max_d and min_d:
            years_of_history = max((datetime.strptime(max_d, "%Y-%m-%d") -
                                    datetime.strptime(min_d, "%Y-%m-%d")).days / 365.0, 0.0)
    except Exception:
        pass
    if years_of_history >= 1.0 and n_dates >= 60 and n_trades >= 20:
        quality = "sufficient"    # 可验证IR=0.5 (≈t≈2)
    elif years_of_history >= 0.08 and n_dates >= 5:  # ~1 month
        quality = "limited"       # 有参考价值但统计不显著
    else:
        quality = "early_stage"   # 数据不足, 数值仅供参考

    metrics = {
        "strategy": strategy,
        "ic_pearson_1d": ic.get("ic_pearson_1d"),
        "ic_pearson_3d": ic.get("ic_pearson_3d"),
        "ic_pearson_5d": ic.get("ic_pearson_5d"),
        "ic_pearson_20d": ic.get("ic_pearson_20d"),
        "ic_spearman_1d": ic.get("ic_spearman_1d"),
        "ic_spearman_3d": ic.get("ic_spearman_3d"),
        "ic_spearman_5d": ic.get("ic_spearman_5d"),
        "ic_spearman_20d": ic.get("ic_spearman_20d"),
        "ir_annualized": ir_val,
        "br_bets_per_year": br_val,
        "ir_implied": ir_implied,
        "ir_benchmark": "Grinold: 0.50良好 | 0.75优秀 | 1.00卓越",
        "n_signals": n_sig,
        "n_trades": n_trades,
        "data_quality": quality,
        "computed_at": datetime.now().isoformat(),
        "survivorship_note": (
            f"⚠️ 幸存者偏差: daily表仅含存续股票, "
            f"已退市/ST/被收购的股票不在库中。"
            f"IC和绩效可能系统性高估。来源: Harris 22章"
        ),
        "risk_alerts": {
            "model_breakdown": bool(ic.get("ic_pearson_1d") is not None and (ic.get("ic_pearson_1d") or 0) < -0.10),
            "drawdown_warning": bool(tc.execute(
                "SELECT COALESCE(SUM(pnl),0) FROM sim_trades WHERE strategy=?",
                (strategy,)).fetchone()[0] < -500),
            "overfitting_warning": None,
            "source": "Narang 第10章 风险内生性"
        },
        "oos_ic_check": None,  # Narang 9章: 样本外验证, 需≥10信号日
    }

    # 写缓存
    tc.execute("""INSERT OR REPLACE INTO strategy_metrics
        (strategy, ic_pearson_1d, ic_pearson_3d, ic_pearson_5d, ic_pearson_20d,
         ic_spearman_1d, ic_spearman_3d, ic_spearman_5d, ic_spearman_20d,
         ir_annualized, br_bets_per_year, ir_implied,
         n_signals, n_trades, data_quality, computed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (metrics["strategy"],
         metrics["ic_pearson_1d"], metrics["ic_pearson_3d"],
         metrics["ic_pearson_5d"], metrics["ic_pearson_20d"],
         metrics["ic_spearman_1d"], metrics["ic_spearman_3d"],
         metrics["ic_spearman_5d"], metrics["ic_spearman_20d"],
         metrics["ir_annualized"], metrics["br_bets_per_year"],
         metrics["ir_implied"],
         metrics["n_signals"], metrics["n_trades"],
         metrics["data_quality"],
         metrics["computed_at"]))
    tc.commit()
    tc.close(); mc.close()
    return metrics


def get_metrics(strategy: str = "chen") -> dict:
    """仅读缓存, 不重算 (用于API快速查询)."""
    tc = sqlite3.connect(TRADE_DB)
    _ensure_table(tc)
    row = tc.execute(
        "SELECT * FROM strategy_metrics WHERE strategy=?", (strategy,)
    ).fetchone()
    if not row:
        tc.close()
        return {"strategy": strategy, "note": "尚未计算, 请先调用compute"}
    cols = [d[1] for d in tc.execute(
        "PRAGMA table_info(strategy_metrics)").fetchall()]
    tc.close()
    return dict(zip(cols, row))


# ═══ MCVA & Alpha转换 (第14章) ═══

# 信号分值统计参数 (从signals表校准, 2026-06-21)
SCORE_MEAN = 0.40    # 信号分数均值 (来源: signals表222条统计)
SCORE_STD  = 0.10    # 信号分数标准差 (来源: signals表222条统计)

# Grinold标准参数
RESIDUAL_VOL_DEFAULT = 0.30   # A股残差波动率年化30% (来源: Grinold表3-1: 20-35%)
IC_PRIOR = 0.05               # 先验IC=0.05 (来源: Grinold 10.7节: 良好信号)
RISK_AVERSION = 0.10          # λ_R=0.10 中等风险厌恶 (来源: Grinold 5.6节)
ACTIVE_RISK_TARGET = 0.05     # ψ=5%目标主动风险 (来源: Grinold 5.4节典型值)

# 交易成本参数 (来源: A股实际费率)
BUY_COST = 0.0008   # 买入成本80bp: 佣金0.03%+滑点0.05% (来源: A股规费+市场微观结构)
SELL_COST = 0.0018  # 卖出成本180bp: 买入成本+印花税0.1% (来源: A股印花税率)


def mode_stats(mode: str = "") -> dict:
    """从signals表读取每个买点类型的分值统计.
    SQLite无STDEV内建函数，取回分数在Python中计算。
    返回 {mode: {'mean': float, 'std': float, 'n': int}} 或单个mode的 {mean, std, n}.
    无数据时回退到全局SCORE_MEAN/SCORE_STD.
    来源: signals表逐mode的实测数据 (222条)"""
    import sqlite3
    tc = sqlite3.connect(TRADE_DB)

    def _compute(scores: list) -> dict:
        n = len(scores)
        if n < 5:
            return {"mean": SCORE_MEAN, "std": SCORE_STD, "n": n}
        m = sum(scores) / n
        var = sum((x - m)**2 for x in scores) / (n - 1)
        return {"mean": round(m, 4), "std": round(math.sqrt(var), 4), "n": n}

    if mode:
        rows = tc.execute(
            "SELECT score FROM signals WHERE mode=? AND score IS NOT NULL", (mode,)
        ).fetchall()
        tc.close()
        return _compute([r[0] for r in rows])

    result = {}
    all_modes = tc.execute(
        "SELECT DISTINCT mode FROM signals WHERE score IS NOT NULL"
    ).fetchall()
    for (m,) in all_modes:
        scores = [r[0] for r in tc.execute(
            "SELECT score FROM signals WHERE mode=? AND score IS NOT NULL", (m,)
        ).fetchall()]
        result[m] = _compute(scores)
    tc.close()
    return result


def alpha_from_score(score: float, residual_vol: float = RESIDUAL_VOL_DEFAULT,
                     ic: float = IC_PRIOR, mode: str = "") -> float:
    """将原始信号分数转换为Grinold Alpha (年化).
    α = ω × IC × z_score
    z_score = (score - mode_mean) / mode_std   (mode已知时)
            = (score - SCORE_MEAN) / SCORE_STD (mode未知时)
    来源: Grinold 式(10-11) + 第11章横截面标准化"""
    if mode:
        stats = mode_stats(mode)
        m, s = stats["mean"], stats["std"]
    else:
        m, s = SCORE_MEAN, SCORE_STD
    z = (score - m) / s if s > 0 else 0
    return residual_vol * ic * z


def mcva(alpha: float, position_pct: float, residual_vol: float = RESIDUAL_VOL_DEFAULT,
         active_risk: float = ACTIVE_RISK_TARGET,
         risk_aversion: float = RISK_AVERSION) -> float:
    """边际附加值贡献.
    MCVA_n = α_n - 2 × λ_A × ψ × h_n × ω²_n
    简化假设: MCAR_n ≈ h_n × ω²_n (集中持仓的主动风险贡献)
    来源: Grinold 式(14-7)"""
    mcar = position_pct * residual_vol ** 2
    return alpha - 2 * risk_aversion * active_risk * mcar


def should_buy(score: float, residual_vol: float = RESIDUAL_VOL_DEFAULT,
               ic: float = IC_PRIOR) -> tuple[bool, float]:
    """MCVA买入判定: alpha > buy_cost.
    返回 (是否买入, alpha值)"""
    a = alpha_from_score(score, residual_vol, ic)
    return a > BUY_COST, a


def should_sell(score: float, position_pct: float = 0.01,
                residual_vol: float = RESIDUAL_VOL_DEFAULT,
                ic: float = IC_PRIOR) -> tuple[bool, float]:
    """MCVA卖出判定: MCVA < -sell_cost.
    返回 (是否卖出, MCVA值)"""
    a = alpha_from_score(score, residual_vol, ic)
    m = mcva(a, position_pct, residual_vol)
    return m < -SELL_COST, m


def mcva_trailing_stop(entry_alpha: float, current_alpha: float,
                       position_pct: float, residual_vol: float = RESIDUAL_VOL_DEFAULT) -> float:
    """MCVA驱动的移动止盈阈值.
    买入时MCVA=entry_cost, 当MCVA< -exit_cost 时卖出.
    alpha区间宽度 = buy_cost + sell_cost + 2λψ×ΔMCAR
    返回: 当前alpha距离卖出线的距离(正值=安全, 负值=触发卖出).
    来源: Grinold 式(14-8/14-9)"""
    mcar_term = 2 * RISK_AVERSION * ACTIVE_RISK_TARGET * position_pct * residual_vol ** 2
    threshold = -SELL_COST - mcar_term
    return current_alpha - threshold  # >0 安全, <0 触发卖出


# ═══ Kelly Criterion (来源: Kelly 1956, Thorp 2006) ═══

def kelly_fraction(strategy: str = "chen", tc: sqlite3.Connection = None,
                   n_positions: int = 1, drawdown_pct: float = 0.0) -> float:
    """Kelly最优仓位比例 (半Kelly + 多仓位折扣 + 回撤缩放).

    f* = (b × p - q) / b × fractional × correlation_discount × drawdown_scale

    来源: Kelly 1956, Thorp 2006, McDonnell Optimal Portfolio Modeling
    补充:
      - 多仓位相关性折扣 (McDonnell): f_adj = f / (1 + ρ×(n-1))
      - 回撤缩放 (Keeks): 回撤>10%后Kelly乘数减半
      - 样本量分档: <20笔=×0.10, <50笔=×0.25, 50-200笔=×0.33, 200+笔=×0.50
    """
    close_db = tc is None
    if close_db:
        tc = sqlite3.connect(TRADE_DB)
    rows = tc.execute(
        "SELECT pnl FROM sim_trades WHERE side='sell' AND strategy=? AND pnl IS NOT NULL",
        (strategy,)).fetchall()
    if close_db:
        tc.close()

    from ops.position_sizers import (PRIOR_WINS, PRIOR_LOSSES,
                                       PRIOR_AVG_WIN, PRIOR_AVG_LOSS)

    n_trades = len(rows)
    if n_trades < 3:
        # 贝叶斯先验 (来源: Grinold "良好策略" 基准 + Chan 示例)
        p_prior = PRIOR_WINS / (PRIOR_WINS + PRIOR_LOSSES)
        b_prior = PRIOR_AVG_WIN / PRIOR_AVG_LOSS
        f_raw = (b_prior * p_prior - (1 - p_prior)) / b_prior
        return min(max(f_raw * 0.10, 0), 0.10)  # 先验驱动, 极度保守(×0.10)

    pnls = [r[0] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    if not losses:
        return 0.10  # 全盈利→保守10%
    if not wins:
        return 0

    p = len(wins) / len(pnls)
    avg_win = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
    if avg_loss == 0:
        return 0
    b = avg_win / avg_loss
    f_raw = (b * p - (1 - p)) / b
    if f_raw <= 0:
        return 0

    # 样本量分档乘数 (来源: Monte Carlo + Kelly 2024)
    if n_trades < 20:
        frac = 0.10
    elif n_trades < 50:
        frac = 0.25
    elif n_trades < 200:
        frac = 0.33
    else:
        frac = 0.50

    # 多仓位相关性折扣 (来源: McDonnell — 同策略多仓位≈高相关)
    if n_positions > 1:
        rho = 0.60  # 同策略(陈小群)持仓间平均相关≈0.6
        frac *= 1.0 / (1 + rho * (n_positions - 1))

    # 回撤缩放 (来源: Keeks — "黄金法则": 回撤>10%→Kelly减半)
    if drawdown_pct > 0.10:
        frac *= 0.50

    kelly_result = min(f_raw * frac, 0.50)

    # 黑色星期一检验 (来源: Chan 第6章 — 杠杆≤历史最大单日亏损倒数)
    if kelly_result > 0:
        max_loss_pct = min(abs(p) / float(cfg("backtest.initial_capital", 5000)) for p in pnls if p < 0) if losses else 0.10
        if max_loss_pct > 0:
            max_safe_leverage = 1.0 / max_loss_pct  # 承受一次最坏亏损
            kelly_result = min(kelly_result, max_safe_leverage)
    return min(kelly_result, 0.50)


# ═══ CLI ═══

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Grinold & Kahn IC/IR/BR 计算")
    p.add_argument("strategy", nargs="?", default="chen",
                   help="策略名 (chen|etf|smallcap|timing), 默认chen")
    p.add_argument("--force", "-f", action="store_true", help="强制重算, 忽略缓存")
    args = p.parse_args()

    strategies = ["chen", "etf", "smallcap", "timing"] if args.strategy == "all" else [args.strategy]
    for s in strategies:
        result = compute_strategy_metrics(s, force=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
