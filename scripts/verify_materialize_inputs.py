"""物化输入数据完整性验证 — 从 start_date 起逐日顺序检查 (非抽查)。

用途: materialize_full 前必须跑, 全绿才允许物化。
scope: 物化依赖的 14 表中, 主数据逐日检查字段值有效性 + 覆盖;
      aux 事件型表 (margin/lhb) 允许缺日; 财务三表按季度检查覆盖率;
      factor 池外的表 (news/analyst/fund_hold/intraday) 不在检查范围。

退出码: 0=通过(可物化), 1=有失败项(不可物化)。
"""

import sqlite3
import sys
import textwrap

from quant.config.paths import MARKET_DB
from quant.utils.logger import get_logger

_log = get_logger("quant.data.verify_inputs")

START_DATE = "2019-01-01"
END_DATE = "2026-08-13"

# ── 阈值 ────────────────────────────────────────────────────────────────
MIN_DAILY_ROWS = 100          # A股单日至少 100+ 只, 低于此判定为缺数据
MIN_VAL_ROW_RATIO = 0.9       # daily_valuation 行数须 ≥ 同日 daily 的 90%
MIN_BENCH_ROWS = 1            # benchmark 000300 每日须有行
MIN_FIN_Q2_ROWS = 1000        # 财务季报覆盖: 该季度至少 1000 只 (全市场 ~5400)
FIN_REPORT_QUARTERS = {       # 2019-2026 应披露季度 (报告期)
    2019: ["Q1", "Q2", "Q3", "Q4"], 2020: ["Q1", "Q2", "Q3", "Q4"],
    2021: ["Q1", "Q2", "Q3", "Q4"], 2022: ["Q1", "Q2", "Q3", "Q4"],
    2023: ["Q1", "Q2", "Q3", "Q4"], 2024: ["Q1", "Q2", "Q3", "Q4"],
    2025: ["Q1", "Q2", "Q3", "Q4"], 2026: ["Q1"],
}
TURNOVER_ZERO_MAX = 0.5        # 单日 turnover>0 比例 < 50% 判缺 (回填中断即 <20%)
VAL_NONNULL_MAX = 0.5         # pe_ttm/market_cap 非空率低于 50% 判为缺数据


def _conn():
    conn = sqlite3.connect(MARKET_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt(v):
    if v is None:
        return "?"
    return f"{v:.1%}" if isinstance(v, float) else str(v)


def check_daily(c) -> list:
    """daily: 每日行数 + close>0 + turnover>0 比例 (值有效性, 非仅行数)."""
    fails = []
    rows = c.execute(textwrap.dedent(f"""
        SELECT date,
               COUNT(*)                  AS n,
               SUM(CASE WHEN close > 0 THEN 1 ELSE 0 END)  AS n_close,
               SUM(CASE WHEN turnover > 0 THEN 1 ELSE 0 END) AS n_to,
               SUM(CASE WHEN amount >= 0 AND volume >= 0 THEN 1 ELSE 0 END) AS n_vol,
               COUNT(*) - SUM(CASE WHEN close > 0 THEN 1 ELSE 0 END) AS n_bad_close,
               COUNT(*) - SUM(CASE WHEN turnover > 0 THEN 1 ELSE 0 END) AS n_zero_to
        FROM daily
        WHERE date BETWEEN ? AND ?
        GROUP BY date ORDER BY date
    """), (START_DATE, END_DATE)).fetchall()

    if not rows:
        fails.append(f"[daily] 无任何行 in {START_DATE}~{END_DATE}")
        return fails

    by_year = {}
    for r in rows:
        y = r["date"][:4]
        by_year.setdefault(y, dict(total=0, min_n=10**9, max_n=0,
                                   to_bad_days=0, close_bad_days=0,
                                   vol_bad_days=0, days=0))
        s = by_year[y]
        s["total"] += r["n"]
        s["days"] += 1
        s["min_n"] = min(s["min_n"], r["n"])
        s["max_n"] = max(s["max_n"], r["n"])

        if r["n"] < MIN_DAILY_ROWS:
            s["to_bad_days"] += 1
            fails.append(f"[daily] {r['date']} 行数 {r['n']} < {MIN_DAILY_ROWS}")
        else:
            to_zero_ratio = (r["n_zero_to"] or 0) / r["n"]
            if to_zero_ratio > TURNOVER_ZERO_MAX:
                s["to_bad_days"] += 1
                fails.append(f"[daily] {r['date']} turnover>0 比例 {_fmt(1-to_zero_ratio)} — 回填中断/缺失")
            if (r["n_bad_close"] or 0) / r["n"] > 0.5:
                s["close_bad_days"] += 1
                fails.append(f"[daily] {r['date']} close<=0 比例 {(r['n_bad_close'] or 0)/r['n']:.0%}")
            if (r["n_vol"] or 0) / r["n"] < 0.5:
                s["vol_bad_days"] += 1
                fails.append(f"[daily] {r['date']} volume/amount<=0 比例 {_fmt(1-(r['n_vol'] or 0)/r['n'])}")

    _log.info("daily 逐日检查完成: %d 交易日, %s 行", len(rows),
              f"{sum(v['total'] for v in by_year.values()):,}")
    for y in sorted(by_year):
        s = by_year[y]
        _log.info("  %s: %d 天 [min=%d max=%d] turnover缺=%d天 close缺=%d天",
                  y, s["days"], s["min_n"], s["max_n"],
                  s["to_bad_days"], s["close_bad_days"])
    return fails


def check_valuation(c) -> list:
    """daily_valuation: 每日行数须 ≥ 同日 daily 90%, 且 pe/mv 非空率 ≥ 50%."""
    fails = []
    daily_n = {r["date"]: r["n"] for r in
               c.execute("SELECT date, COUNT(*) AS n FROM daily "
                         f"WHERE date BETWEEN ? AND ? GROUP BY date",
                         (START_DATE, END_DATE)).fetchall()}
    rows = c.execute(textwrap.dedent(f"""
        SELECT v.date,
               COUNT(*) AS n,
               COUNT(*) - SUM(CASE WHEN v.pe_ttm > 0 THEN 1 ELSE 0 END) AS n_nul_pe,
               COUNT(*) - SUM(CASE WHEN v.market_cap > 0 THEN 1 ELSE 0 END) AS n_nul_mv,
               COUNT(*) - SUM(CASE WHEN v.pb > 0 THEN 1 ELSE 0 END) AS n_nul_pb
        FROM daily_valuation v
        WHERE v.date BETWEEN ? AND ?
        GROUP BY v.date ORDER BY v.date
    """), (START_DATE, END_DATE)).fetchall()

    if not rows:
        fails.append(f"[daily_valuation] 无行")
        return fails

    n_pe_bad = n_ratio_bad = 0
    for r in rows:
        n_daily = daily_n.get(r["date"], 0)
        if n_daily and r["n"] / n_daily < MIN_VAL_ROW_RATIO:
            n_ratio_bad += 1
            if n_ratio_bad <= 3:
                fails.append(f"[daily_valuation] {r['date']} 行数 {r['n']} < daily "
                             f"{n_daily} 的 {_fmt(r['n']/n_daily)}")
        if (r["n"] and (r["n_nul_pe"] or 0) / r["n"] > (1 - VAL_NONNULL_MAX)):
            n_pe_bad += 1
            if n_pe_bad <= 3:
                fails.append(f"[daily_valuation] {r['date']} pe_ttm 非空率 {(r['n']-(r['n_nul_pe'] or 0))/r['n']:.0%} < {VAL_NONNULL_MAX}")
    if n_ratio_bad > 3:
        fails.append(f"[daily_valuation] 另有 {n_ratio_bad-3} 天覆盖不足 (按年摘要见下)")
    _log.info("daily_valuation: %d 天, 覆盖不足 %d 天, pe 非空不足 %d 天",
              len(rows), n_ratio_bad, n_pe_bad)
    return fails


def check_benchmark(c) -> list:
    fails = []
    rows = c.execute("""
        SELECT date, COUNT(*) AS n, COUNT(*) - SUM(CASE WHEN close > 0 THEN 1 ELSE 0 END) AS n_bad
        FROM benchmark_daily
        WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date
    """, (START_DATE, END_DATE)).fetchall()
    if not rows:
        fails.append("[benchmark_daily] 000300 无数据")
        return fails
    # 找出工作日内 benchmark 缺失的连续段 (简化: 与 daily 对齐)
    miss = c.execute(textwrap.dedent(f"""
        SELECT d.date FROM daily d
        WHERE d.date BETWEEN ? AND ?
          AND NOT EXISTS (SELECT 1 FROM benchmark_daily b WHERE b.date = d.date)
        GROUP BY d.date
    """), (START_DATE, END_DATE)).fetchall()
    if miss:
        dates = sorted(r["date"] for r in miss)
        _log.info("benchmark 缺失 %d 个交易日 (daily 有, benchmark 无): %s ... %s",
                  len(dates), dates[0], dates[-1])
        fails.append(f"[benchmark_daily] {len(dates)} 个交易日缺失 (首末: {dates[0]} / {dates[-1]})")
    return fails


def check_financial(c) -> list:
    """财务三表: 按报告期(stat_date)统计覆盖, 每季 ≥ MIN_FIN_Q2_ROWS."""
    fails = []
    # 2019-2022 financial 表 stat_date 应完整; 判定季度覆盖按 stat_date 月份直接映射
    period = {"Q1": "03", "Q2": "06", "Q3": "09", "Q4": "12"}
    for tbl, col in [("financial_income", "net_profit"),
                     ("financial_balance", "total_assets"),
                     ("financial_cashflow", "net_operate_cash_flow")]:
        rows = c.execute(f"""
            SELECT substr(stat_date, 1, 4) AS y, substr(stat_date, 6, 2) AS m,
                   COUNT(*) AS n,
                   COUNT(*) - SUM(CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END) AS n_nul,
                   COUNT(DISTINCT symbol) AS nsym
            FROM {tbl}
            GROUP BY y, m ORDER BY y, m
        """).fetchall()
        cov = {(r["y"], r["m"]): r for r in rows}
        for y, qs in FIN_REPORT_QUARTERS.items():
            for q in qs:
                m = period[q]
                r = cov.get((str(y), m))
                n = r["n"] if r else 0
                if n < MIN_FIN_Q2_ROWS:
                    fails.append(f"[{tbl}] {y}{q} 仅 {n} 行 (应 ≥ {MIN_FIN_Q2_ROWS}, "
                                 f"全市场 ≈5400)")
        _log_result = {k: v["n"] for k, v in sorted(cov.items())}
        _log.info("%s 覆盖: %s", tbl, _log_result)
    return fails


def check_margin_lhb(c) -> list:
    """margin/lhb 事件型: 有数据段即可, 校验非负/非空."""
    fails = []
    rng = c.execute("SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM margin_detail").fetchone()
    _log.info("margin_detail: %s ~ %s, %d 个交易日", rng[0], rng[1], rng[2])
    if rng[2] and rng[2] < 50:
        fails.append(f"[margin_detail] 仅 {rng[2]} 个交易日, 疑似严重缺失")

    neg = c.execute("SELECT COUNT(*) FROM margin_detail WHERE margin_balance < 0 OR short_balance < 0").fetchone()[0]
    if neg:
        fails.append(f"[margin_detail] {neg} 行负数 (margin_balance/short_balance)")

    b = c.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM lhb_detail").fetchone()
    _log.info("lhb_detail: %s ~ %s, %d 行 (事件型, 允许缺日)", b[0], b[1], b[2])
    nul = c.execute("SELECT COUNT(*) FROM lhb_detail WHERE date(trade_date) IS NULL").fetchone()[0]
    if nul:
        fails.append(f"[lhb_detail] {nul} 行 trade_date 为空")
    return fails


def check_stocks(c) -> list:
    fails = []
    r = c.execute("""
        SELECT COUNT(*) AS n,
               COUNT(*) - SUM(CASE WHEN industry IS NOT NULL AND industry != '' THEN 1 ELSE 0 END) AS n_ind,
               COUNT(*) - SUM(CASE WHEN roe IS NOT NULL THEN 1 ELSE 0 END) AS n_roe,
               COUNT(*) - SUM(CASE WHEN eps IS NOT NULL THEN 1 ELSE 0 END) AS n_eps,
               COUNT(*) - SUM(CASE WHEN bvps IS NOT NULL THEN 1 ELSE 0 END) AS n_bvps,
               COUNT(*) - SUM(CASE WHEN total_mv > 0 THEN 1 ELSE 0 END) AS n_mv
        FROM stocks
    """).fetchone()
    _log.info("stocks: %d 只, industry缺%d roe缺%d eps缺%d bvps缺%d mv缺%d",
              r["n"], r["n_ind"], r["n_roe"], r["n_eps"], r["n_bvps"], r["n_mv"])
    if r["n_ind"] and r["n_ind"] / r["n"] > 0.2:
        fails.append(f"[stocks] industry 缺失率 {r['n_ind']/r['n']:.0%} > 20%")
    if r["n_mv"] and r["n_mv"] / r["n"] > 0.2:
        fails.append(f"[stocks] total_mv 缺失率 {r['n_mv']/r['n']:.0%} > 20% (市值中性化依赖)")
    return fails


def main():
    import argparse
    global START_DATE, END_DATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START_DATE)
    ap.add_argument("--end", default=END_DATE)
    args = ap.parse_args()

    START_DATE, END_DATE = args.start, args.end

    _log.info("物化输入验证开始: %s ~ %s", START_DATE, END_DATE)
    c = _conn()
    all_fails = []
    for fn, name in [(check_daily, "daily"),
                     (check_valuation, "daily_valuation"),
                     (check_benchmark, "benchmark_daily"),
                     (check_financial, "financial 三表"),
                     (check_margin_lhb, "margin/lhb"),
                     (check_stocks, "stocks")]:
        try:
            all_fails += fn(c)
        except Exception as e:  # 零fallback: 单表检查失败直接算失败, 不吞
            all_fails.append(f"[{name}] 检查异常: {type(e).__name__}: {e}")
    c.close()

    _log.info("═" * 60)
    if all_fails:
        _log.error("✗ 验证失败 %d 项 — 物化被阻断:", len(all_fails))
        for f in all_fails:
            _log.error("  └ %s", f)
        print("VERIFY_FAIL")
        sys.exit(1)
    _log.info("✓ 全绿 — 允许物化")
    print("VERIFY_PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
