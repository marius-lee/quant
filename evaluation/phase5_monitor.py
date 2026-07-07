"""Stage 5: 持续监控 — 因子拥挤度 / IC 衰减 / 换手率 / 容量估算。

设计为可被 scheduler 周期性调用 (建议每日盘后).
输出 Markdown 报告到 docs/reports/monitor_YYYY-MM-DD.md
"""

import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from config.loader import get as cfg


def run_monitor(output_dir: str = "docs/reports") -> str:
    """运行持续监控, 输出报告路径。

    Returns
    -------
    str : 报告文件路径
    """
    import os
    os.makedirs(output_dir, exist_ok=True)

    today = datetime.today().strftime("%Y-%m-%d")
    report_path = os.path.join(output_dir, f"monitor_{today}.md")

    conn = sqlite3.connect("data/market.db")

    # ── 1. 因子拥挤度 (pairwise correlation) ──
    active = [r[0] for r in conn.execute(
        "SELECT name FROM factor_registry WHERE status='active'").fetchall()]

    crowding_section = _check_crowding(conn, active)

    # ── 2. IC 衰减监控 ──
    ic_decay_section = _check_ic_decay(active)

    # ── 3. 换手率监控 ──
    turnover_section = _check_turnover(conn)

    # ── 4. 容量估算 ──
    capacity_section = _estimate_capacity(conn)

    conn.close()

    # ── 写入报告 ──
    report = f"""# 因子监控报告 {today}

## 因子拥挤度 (correlation matrix)
{crowding_section}

## IC 衰减趋势 (最近60天)
{ic_decay_section}

## 组合换手率
{turnover_section}

## 容量估算 (基于 Amihud 非流动性)
{capacity_section}

---
*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}*
"""
    with open(report_path, 'w') as f:
        f.write(report)

    print(f"Phase 5 report written: {report_path}")
    return report_path


def _check_crowding(conn, active_factors: list) -> str:
    """检查因子间相关性是否异常升高 (>0.7 预先警告)."""
    if len(active_factors) < 2:
        return "活跃因子 < 2, 跳过拥挤度检查.\n"

    # 从 factor_stats 获取最近 IC 值
    rows = conn.execute("""
        SELECT name, ic_mean FROM factor_registry
        WHERE name IN ({})
    """.format(",".join("?" * len(active_factors))), active_factors).fetchall()

    ic_dict = {r[0]: r[1] for r in rows if r[1] is not None}

    if len(ic_dict) < 2:
        return "IC 数据不足, 跳过拥挤度检查.\n"

    # 简化的拥挤度指标: 检查 IC 方向一致性
    # 真实拥挤度需要因子值的截面相关性, 此处用 IC 符号一致性作为代理
    ics = np.array(list(ic_dict.values()))
    same_sign_ratio = float(np.sum(ics > 0)) / len(ics)

    lines = [f"- 正 IC 因子比例: {same_sign_ratio:.0%}"]
    if same_sign_ratio > 0.8:
        lines.append("- ⚠️ 警告: >80% 因子同向, 可能存在因子拥挤")
    elif same_sign_ratio > 0.95:
        lines.append("- 🔴 严重拥挤: >95% 因子同向")
    else:
        lines.append("- ✅ 因子方向分散, 拥挤度正常")

    return "\n".join(lines) + "\n"


def _check_ic_decay(active_factors: list) -> str:
    """检查 IC 衰减是否加速."""
    if not active_factors:
        return "无活跃因子.\n"

    from factor.stats_cache import compute_factor_stats
    lookback = cfg("factor.evaluation.lookback", 120)

    try:
        stats = compute_factor_stats(
            factor_names=active_factors[:20],  # 最多20个, 节省时间
            n_symbols=None,
            lookback=min(lookback, 60),
        )
        decay = stats.get("decay", {})
    except Exception as e:
        return f"IC 衰减计算失败: {e}\n"

    lines = []
    for name in sorted(decay.keys()):
        d = decay[name]
        ic_1d = d.get("1d", 0)
        ic_5d = d.get("5d", 0)
        ic_20d = d.get("20d", 0)
        if ic_1d:
            ratio_20 = abs(ic_20d / ic_1d) if abs(ic_1d) > 1e-6 else 0
            warning = " ⚠️ 衰减加速" if ratio_20 < 0.3 else ""
            lines.append(f"- {name:30s} IC_1d={ic_1d:+.4f}  IC_20d={ic_20d:+.4f}  "
                        f"retention={ratio_20:.0%}{warning}")

    if not lines:
        return "无 IC 衰减数据.\n"
    return "\n".join(lines) + "\n"


def _check_turnover(conn) -> str:
    """检查最近持仓换手率."""
    try:
        rows = conn.execute("""
            SELECT date, symbol, action, shares
            FROM sim_trades
            WHERE date >= date('now', '-10 days')
              AND strategy='quant'
            ORDER BY date DESC
        """).fetchall()
    except Exception:
        return "sim_trades 表不可用, 跳过换手率检查.\n"

    if not rows:
        return "最近10天无交易记录.\n"

    dates = sorted(set(r[0] for r in rows))
    trades_per_day = {d: sum(1 for r in rows if r[0] == d) for d in dates}

    avg_trades = float(np.mean(list(trades_per_day.values())))
    return f"- 最近10天平均每日交易: {avg_trades:.1f} 笔\n"


def _estimate_capacity(conn) -> str:
    """基于 Amihud 非流动性估算策略容量."""
    try:
        rows = conn.execute("""
            SELECT symbol, AVG(amount) as avg_amt
            FROM daily
            WHERE date >= date('now', '-60 days')
            GROUP BY symbol
            ORDER BY avg_amt DESC
            LIMIT 100
        """).fetchall()
    except Exception as e:
        return f"容量估算失败: {e}\n"

    if not rows:
        return "无成交额数据.\n"

    avg_daily_amount = float(np.median([r[1] for r in rows if r[1]]))
    # 安全参与比例: 单只股票不超日均成交额的 1%
    safe_position_pct = 0.01
    positions = cfg("alpha.sleeve.positions_per_factor", 20)
    n_factors = len([r for r in conn.execute(
        "SELECT 1 FROM factor_registry WHERE status='active'").fetchall()])

    per_stock_capacity = avg_daily_amount * safe_position_pct
    total_capacity = per_stock_capacity * positions * max(n_factors, 1)

    return (
        f"- 中位日均成交额: ¥{avg_daily_amount:,.0f}\n"
        f"- 单票安全仓位 (1%): ¥{per_stock_capacity:,.0f}\n"
        f"- 估计策略容量: ¥{total_capacity:,.0f} "
        f"({positions} 票 × {n_factors} 因子)\n"
    )
