"""月度板块热度扫描 — 每月1号自动运行.
来源: BigQuant四维模型 (涨停数+涨幅+资金+连板龙)
"""
import sqlite3, json, os
from collections import defaultdict
from datetime import date, timedelta

DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")

def scan(last_month: str = None):
    if last_month is None:
        today = date.today()
        first = today.replace(day=1)
        last = first - timedelta(days=1)
        last_month = last.strftime("%Y-%m")

    conn = sqlite3.connect(DB)
    # 上月所有涨停股
    rows = conn.execute("""
        SELECT symbol, close, (close-prev.close)/prev.close as ret
        FROM daily JOIN daily prev ON daily.symbol=prev.symbol AND prev.date=(
            SELECT MAX(date) FROM daily WHERE symbol=daily.symbol AND date < daily.date
        ) WHERE daily.date LIKE ?
    """, (f"{last_month}%",)).fetchall()

    # 按efinance板块归组(需要运行时import, 离线用Shenwan近似)
    sector_stats = defaultdict(lambda: {"涨停": 0, "涨幅": 0.0, "成交额": 0.0, "count": 0})
    for sym, close, ret in rows:
        if ret and ret >= 0.095:
            # 简化为全市场统计, 实际应查efinance
            sector_stats["ALL"]["涨停"] += 1
            sector_stats["ALL"]["涨幅"] += ret * 100
            sector_stats["ALL"]["count"] += 1

    conn.close()

    # 输出 Top 10
    result = {"month": last_month, "top_sectors": [], "suggestions": []}
    print(f"上月({last_month})热门板块扫描完成")

    # 对比产业链映射表
    tc = sqlite3.connect(TRADE_DB)
    chains = {}
    for r in tc.execute("SELECT chain_name, level, sector_name FROM industry_chains").fetchall():
        chains.setdefault(r[0], set()).add(r[2])
    tc.close()
    print(f"现有产业链: {list(chains.keys())}")
    print("(月度扫描完整版需接入efinance, 当前为框架)")

    return result

if __name__ == "__main__":
    scan()