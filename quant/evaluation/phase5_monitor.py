"""Stage 5: 持续监控 — 因子拥挤度 / IC 衰减 / 换手率 / 容量估算。

设计为可被 scheduler 周期性调用 (建议每日盘后).
输出 Markdown 报告到 docs/reports/monitor_YYYY-MM-DD.md
"""

import sqlite3
import traceback
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger, set_trace_id
from quant.data.repos._base import DatabaseManager


def run_monitor(output_dir: str = "docs/reports") -> str:
    """运行持续监控, 输出报告路径。

    Returns
    -------
    str : 报告文件路径
    """
    import uuid; tid = uuid.uuid4().hex[:12]; set_trace_id(tid)
    logger = get_logger("evaluation.phase5")
    import os
    os.makedirs(output_dir, exist_ok=True)

    today = datetime.today().strftime("%Y-%m-%d")
    logger.info(f"Phase 5 [{tid}] start — monitoring report")
    report_path = os.path.join(output_dir, f"monitor_{today}.md")

    conn = DatabaseManager.get_instance().get_connection("quant/data/market.db")

    # ── 1. 因子拥挤度 (pairwise correlation) ──
    active = [r[0] for r in conn.execute(
        "SELECT name FROM factor_registry WHERE status IN ('active','monitoring')").fetchall()]

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

    logger.info(f"Phase 5 report written: {report_path}")
    return report_path


def _check_crowding(conn, active_factors: list) -> str:
    """G2: 因子拥挤度 — 截面因子值的 pairwise Pearson 相关性.

    真实拥挤度检测 (非 IC 符号代理):
    1. 计算最近交易日各因子的截面因子值
    2. 计算因子间的 pairwise Pearson 相关性矩阵
    3. 标记 |r| > 0.7 的因子对 → 可能拥挤
    4. 统计每个因子的高相关邻居数 → 拥挤风险排名

    参考: MSCI (2018) "Crowd Control"; Lee (2025) "双曲线衰减模型"
    """
    if len(active_factors) < 2:
        return "活跃因子 < 2, 跳过拥挤度检查.\n"

    try:
        from datetime import datetime
        from quant.data.store import DataStore
        from quant.factor.compute import compute_all_factors
        from quant.data.repos import UniverseRepo

        today = datetime.today().strftime("%Y-%m-%d")
        store = DataStore()
        symbols = UniverseRepo().get_symbols(exclude_market='BJ')[:500]
        data = store.get_daily(symbols, start=(pd.Timestamp(today) - pd.Timedelta(days=30)).strftime("%Y-%m-%d"), end=today)
        fundamentals = store.get_fundamentals(symbols, date=today)

        if data.empty:
            store.close()
            return "数据不足, 跳过拥挤度检查.\n"

        factor_values = compute_all_factors(data, today, fundamentals=fundamentals,
                                            status_filter="using")
        factor_values = {k: v for k, v in factor_values.items()
                        if isinstance(v, pd.Series) and v.notna().sum() >= 30}

        store.close()

        if len(factor_values) < 2:
            return "有效因子值 < 2, 跳过拥挤度检查.\n"

        # ── 构建因子值矩阵 (symbol × factor) ──
        factor_df = pd.DataFrame(factor_values).dropna(how="all")
        factor_df = factor_df.dropna(axis=1, thresh=30)

        if factor_df.shape[1] < 2:
            return "因子值覆盖不足, 跳过拥挤度检查.\n"

        # ── Pairwise Pearson 相关性 ──
        corr_matrix = factor_df.corr(method="pearson")

        # ── 找高相关对 ──
        high_corr_pairs = []
        n = corr_matrix.shape[0]
        for i in range(n):
            for j in range(i + 1, n):
                r = corr_matrix.iloc[i, j]
                if abs(r) > 0.7:
                    high_corr_pairs.append((corr_matrix.index[i], corr_matrix.index[j], round(r, 3)))

        # ── 拥挤风险排名 (每个因子有多少邻居 >0.7) ──
        crowded_factors = {}
        for f1, f2, r in high_corr_pairs:
            crowded_factors[f1] = crowded_factors.get(f1, 0) + 1
            crowded_factors[f2] = crowded_factors.get(f2, 0) + 1

        lines = []
        lines.append(f"- 分析因子数: {factor_df.shape[1]}, 截面股票数: {factor_df.shape[0]}")

        if high_corr_pairs:
            lines.append(f"- 🔴 高相关对 (|r|>0.7): {len(high_corr_pairs)} 对")
            for f1, f2, r in sorted(high_corr_pairs, key=lambda x: -abs(x[2]))[:8]:
                lines.append(f"  - {f1} ↔ {f2}: r={r:+.3f}")

            lines.append("")
            lines.append("- 拥挤风险排名 (高相关邻居数):")
            for fname, n_crowd in sorted(crowded_factors.items(), key=lambda x: -x[1])[:5]:
                risk = "🔴 高风险" if n_crowd >= 3 else ("⚠️ 中风险" if n_crowd >= 2 else "低风险")
                lines.append(f"  - {fname}: {n_crowd} 个高相关邻居 ({risk})")
        else:
            lines.append("- ✅ 未检测到高相关性拥挤 (所有 |r| < 0.7)")

    except Exception as e:
        lines = [f"- 拥挤度检查失败 (non-fatal): {type(e).__name__}: {e}"]

    return "\n".join(lines) + "\n"


def _check_ic_decay(active_factors: list) -> str:
    """检查 IC 衰减是否加速."""
    if not active_factors:
        return "无活跃因子.\n"

    from quant.factor.stats_cache import compute_factor_stats
    lookback = _require_cfg("factor.evaluation.lookback")

    stats = compute_factor_stats(
        factor_names=active_factors[:20],  # 最多20个, 节省时间
        n_symbols=None,
        lookback=min(lookback, 60),
    )
    decay = stats.get("decay", {})

    lines = []
    for name in sorted(decay.keys()):
        d = decay[name]
        # decay format: {display_name: [ic_1d, ic_5d, ic_20d]} or {name: {"1d": v, "5d": v, "20d": v}}
        if isinstance(d, list) and len(d) >= 3:
            ic_1d, ic_5d, ic_20d = d[0], d[1], d[2]
        elif isinstance(d, dict):
            ic_1d = d.get("1d", 0)
            ic_5d = d.get("5d", 0)
            ic_20d = d.get("20d", 0)
        else:
            continue
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
    rows = conn.execute("""
        SELECT date, symbol, action, shares
        FROM sim_trades
        WHERE date >= date('now', '-10 days')
          AND strategy='quant'
        ORDER BY date DESC
    """).fetchall()

    if not rows:
        return "最近10天无交易记录.\n"

    dates = sorted(set(r[0] for r in rows))
    trades_per_day = {d: sum(1 for r in rows if r[0] == d) for d in dates}

    avg_trades = float(np.mean(list(trades_per_day.values())))
    return f"- 最近10天平均每日交易: {avg_trades:.1f} 笔\n"


def _estimate_capacity(conn) -> str:
    """基于 Amihud 非流动性估算策略容量."""
    rows = conn.execute("""
        SELECT symbol, AVG(amount) as avg_amt
        FROM daily
        WHERE date >= date('now', '-60 days')
        GROUP BY symbol
        ORDER BY avg_amt DESC
        LIMIT 100
    """).fetchall()

    if not rows:
        return "无成交额数据.\n"

    avg_daily_amount = float(np.median([r[1] for r in rows if r[1]]))
    # 安全参与比例: 单只股票不超日均成交额的 1%
    safe_position_pct = 0.01
    positions = _require_cfg("alpha.sleeve.positions_per_factor")
    n_factors = len([r for r in conn.execute(
        "SELECT 1 FROM factor_registry WHERE status IN ('active','monitoring')").fetchall()])

    per_stock_capacity = avg_daily_amount * safe_position_pct
    total_capacity = per_stock_capacity * positions * max(n_factors, 1)

    return (
        f"- 中位日均成交额: ¥{avg_daily_amount:,.0f}\n"
        f"- 单票安全仓位 (1%): ¥{per_stock_capacity:,.0f}\n"
        f"- 估计策略容量: ¥{total_capacity:,.0f} "
        f"({positions} 票 × {n_factors} 因子)\n"
    )


def sync_factor_status() -> dict:
    """Phase 5b: 从 evaluation_runs 读评估结果, 同步到 factor_registry.status。

    不碰已有 status='active' 的因子 (已认证的线上因子)。
    只处理 backtesting 状态 (registered/candidate/retired/rejected) 的因子。

    Returns dict: {rejected: [names], active: [names], unchanged: int}
    """
    import sqlite3
    from quant.evaluation.run_store import load_latest
    from quant.utils.logger import get_logger
    logger = get_logger("evaluation.phase5")

    p2 = load_latest("phase2")
    p3 = load_latest("phase3")
    p4 = load_latest("phase4")

    if not p2:
        logger.warning("sync_factor_status: no Phase 2 data — skipping")
        return {"rejected": [], "active": [], "unchanged": 0}

    # ── 探伤断点: 全零 IC 守卫 ──
    # 如果 Phase 2 所有因子 IC 均为 0.0000 且 0 passed，说明 IC 计算本身可能
    # 出了问题（超时/数据缺失/bug），不是因子真的无效。拒绝同步，保留因子原状态。
    p2_ic_means = p2.get("ic_means", {})
    p2_n_factors = p2.get("n_factors", 0)
    p2_n_passed = len(p2.get("passed", []))
    if (p2_n_factors > 4
            and p2_n_passed == 0
            and p2_ic_means
            and all(abs(v) < 1e-10 for v in p2_ic_means.values())):
        logger.critical(
            "sync_factor_status: CIRCUIT BREAKER — all %d factors have IC≈0.0000, "
            "Phase 2 IC computation likely broken. Refusing to sync. "
            "Fix Phase 2 and re-run evaluation.",
            p2_n_factors
        )
        return {"rejected": [], "active": [], "unchanged": 0, "circuit_breaker": True}

    # Phase 2: failed list
    p2_failed = set(p2.get("failed", {}).keys()) if isinstance(p2.get("failed"), dict) else set(p2.get("failed", []))
    p2_passed = set(p2.get("passed", []))

    # Phase 3: kept list (passed CPCV+PBO)
    p3_kept = set(p3.get("kept", [])) if p3 else set()

    # Phase 4: final certified
    p4_final = set(p4.get("final_factors", [])) if p4 else set()

    # Factors that made it through all phases
    certified = p2_passed & p3_kept & p4_final if p3 and p4 else set()

    # Factors that failed at some phase
    rejected_phase2 = p2_failed
    rejected_phase3 = (p2_passed - p3_kept) if p3 else set()
    rejected_phase4 = ((p2_passed & p3_kept) - p4_final) if p4 else set()

    all_rejected = rejected_phase2 | rejected_phase3 | rejected_phase4

    conn = DatabaseManager.get_instance().get_connection("quant/data/market.db")
    # Get current active factors (don't touch these)
    current_active = set(r[0] for r in conn.execute(
        "SELECT name FROM factor_registry WHERE status='active'"
    ).fetchall())

    # Only act on non-active factors
    rejected_to_update = all_rejected - current_active
    active_to_update = certified - current_active

    # Build reasons
    reasons = {}
    for name in rejected_to_update:
        if name in rejected_phase2:
            reasons[name] = "Phase 2: IC/ICIR/t/half-life thresholds not met"
        elif name in rejected_phase3:
            reasons[name] = "Phase 3: CPCV OOS_ICIR<0 or PBO>threshold"
        elif name in rejected_phase4:
            reasons[name] = "Phase 4: net-of-costs Sharpe too low"
        else:
            reasons[name] = "failed evaluation"
    for name in active_to_update:
        reasons[name] = "passed Phase 2+3+4 (full evaluation)"

    # Update rejected
    for name in rejected_to_update:
        reason = reasons[name]
        conn.execute(
            "UPDATE factor_registry SET status='rejected', status_reason=?, "
            "updated_at=datetime('now','localtime') WHERE name=?",
            (reason, name)
        )

    # Update active
    for name in active_to_update:
        reason = reasons[name]
        conn.execute(
            "UPDATE factor_registry SET status='active', status_reason=?, "
            "updated_at=datetime('now','localtime') WHERE name=?",
            (reason, name)
        )

    conn.commit()
    conn.close()

    logger.info(f"sync_factor_status: {len(rejected_to_update)} rejected, "
                f"{len(active_to_update)} active, {len(current_active)} unchanged")
    for name in sorted(rejected_to_update):
        logger.info(f"  rejected: {name} — {reasons[name]}")
    for name in sorted(active_to_update):
        logger.info(f"  active:   {name} — {reasons[name]}")

    return {
        "rejected": sorted(rejected_to_update),
        "active": sorted(active_to_update),
        "unchanged": len(current_active),
    }
