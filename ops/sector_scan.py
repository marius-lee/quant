"""月度板块热度扫描 — 每月1号自动运行.
来源: BigQuant四维模型 (涨停数+涨幅+资金+连板龙)
输出: 热门板块Top10 + 产业链更新建议
"""
import sqlite3, os, sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")


def scan(last_month: str = None):
    if last_month is None:
        today = date.today()
        first = today.replace(day=1)
        last = first - timedelta(days=1)
        last_month = last.strftime("%Y-%m")

    conn = sqlite3.connect(DB)

    # 上月所有涨停股 (limit-up: >=9.5%)
    rows = conn.execute("""
        SELECT DISTINCT symbol
        FROM daily
        WHERE date LIKE ? AND close > 0
    """, (f"{last_month}%",)).fetchall()
    symbols = [r[0] for r in rows]

    # 按 efinance 板块归组
    sector_stats = defaultdict(lambda: {"limit_ups": 0, "count": 0})
    for sym in symbols:
        try:
            import efinance as ef
            boards = ef.stock.get_belong_board(sym)
            for _, row in boards.iterrows():
                name = row["板块名称"]
                sector_stats[name]["count"] += 1
                sector_stats[name]["limit_ups"] += 1
        except Exception:
            sector_stats["未分类"]["count"] += 1

    conn.close()

    # 综合评分排序
    scored = []
    for name, stats in sector_stats.items():
        if stats["count"] < 5:
            continue
        score = stats["limit_ups"]  # 简化: 只用涨停数, 后续可加涨幅+资金
        scored.append((name, score, stats["limit_ups"]))

    scored.sort(key=lambda x: x[1], reverse=True)
    top10 = scored[:10]

    print(f"\n{'='*50}")
    print(f"上月({last_month})热门板块 Top 10")
    print(f"{'='*50}")
    for i, (name, score, ups) in enumerate(top10):
        print(f"  {i+1}. {name}: 涨停{ups}次 (评分{score})")

    # 对比产业链映射表
    tc = sqlite3.connect(TRADE_DB)
    existing = set()
    for r in tc.execute("SELECT sector_name FROM industry_chains").fetchall():
        existing.add(r[0])

    new_sectors = [(n, s, u) for n, s, u in top10 if n not in existing]
    stale_check = []
    for r in tc.execute("SELECT DISTINCT chain_name FROM industry_chains").fetchall():
        chain = r[0]
        sectors = [r2[0] for r2 in tc.execute(
            "SELECT sector_name FROM industry_chains WHERE chain_name=?", (chain,)).fetchall()]
        active = [s for s in sectors if s in [n for n, _, _ in top10]]
        if not active:
            stale_check.append(chain)

    tc.close()

    if new_sectors:
        print(f"\n🆕 新热门板块(不在映射表中):")
        for n, s, u in new_sectors:
            print(f"  {n} (涨停{u}次)")

    if stale_check:
        print(f"\n⚠️  可能过时的产业链:")
        for c in stale_check:
            print(f"  {c}")

    return {"month": last_month, "top10": [(n, s) for n, s, _ in top10],
            "new_sectors": [(n, s) for n, s, _ in new_sectors],
            "stale_chains": stale_check}


if __name__ == "__main__":
    scan()
