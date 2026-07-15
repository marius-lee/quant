from quant.config.paths import MARKET_DB
#!/usr/bin/env python3
"""数据源初始化脚本 — 一键拉取全A股基础数据。

运行:
  PYTHONPATH=. python3 scripts/init_data.py              # 全部初始化
  PYTHONPATH=. python3 scripts/init_data.py --industry   # 仅行业分类
  PYTHONPATH=. python3 scripts/init_data.py --benchmark  # 仅基准指数
  PYTHONPATH=. python3 scripts/init_data.py --check      # 仅检查数据状态

数据源:
  股票列表: akshare stock_info_a_code_name() (免费, 无需 token)
  行业分类: baostock (Python ≤3.12) 或 akshare THS 行业 (Python 3.14)
  日线OHLCV: Sina → Tencent → akshare 多源回退
  基本面PE/PB: akshare stock_a_lg_indicator()
  基准指数: akshare stock_zh_index_daily()
  实时行情: Sina finance (无需 token)
"""

import sys, os, time, argparse

# Ensure project root in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant.utils.logger import get_logger
logger = get_logger("init_data")


def check_data_status():
    """打印当前数据状态。"""
    import sqlite3
from data.repos._base import DatabaseManager
    db = MARKET_DB
    if not os.path.exists(db):
        print("market.db 不存在 — 尚未初始化")
        return
    conn = DatabaseManager.get_instance().get_connection(db)
    stocks = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
    daily = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    symbols = conn.execute("SELECT COUNT(DISTINCT symbol) FROM daily").fetchone()[0]
    ind_classified = conn.execute("SELECT COUNT(*) FROM stocks WHERE industry IS NOT NULL").fetchone()[0]
    pe_valid = conn.execute("SELECT COUNT(*) FROM stocks WHERE pe IS NOT NULL").fetchone()[0]
    pb_valid = conn.execute("SELECT COUNT(*) FROM stocks WHERE pb IS NOT NULL").fetchone()[0]
    mv_valid = conn.execute("SELECT COUNT(*) FROM stocks WHERE total_mv IS NOT NULL").fetchone()[0]
    date_range = conn.execute("SELECT MIN(date), MAX(date) FROM daily").fetchone()
    conn.close()

    print(f"""
数据状态报告:
  ┌─────────────────────────────┐
  │ 股票列表 (stocks):  {stocks:>6} 只    │
  │ 日线数据 (daily):   {daily:>8} 行   │
  │ 覆盖股票数:         {symbols:>6} 只    │
  │ 日期范围:           {date_range[0]} ~ {date_range[1]} │
  │ 行业已分类:         {ind_classified:>6} / {stocks} │
  │ PE 有效:            {pe_valid:>6} / {stocks} │
  │ PB 有效:            {pb_valid:>6} / {stocks} │
  │ 总市值有效:         {mv_valid:>6} / {stocks} │
  └─────────────────────────────┘
""")


def init_stock_list():
    """初始化股票列表。"""
    from data.store import DataStore
    store = DataStore()
    n = store.sync_stock_list()
    store.close()
    return n


def init_industry():
    """初始化行业分类。"""
    from data.store import DataStore
    store = DataStore()
    n = store.sync_industry()
    store.close()
    return n


def init_daily():
    """增量更新日线 (仅拉取缺失数据)。"""
    from data.store import DataStore
    store = DataStore()
    n = store.update_daily()
    store.close()
    return n


def init_fundamentals():
    """初始化基本面数据 (PE/PB/总市值)。"""
    from data.fundamental import sync_all_fundamentals
    return sync_all_fundamentals()


def init_benchmark():
    """初始化基准指数数据。"""
    from data.benchmark import sync_benchmark
    total = 0
    for code in ["000300", "000905", "000016"]:
        total += sync_benchmark(code)
    return total


def init_all():
    """完整初始化流程。"""
    steps = [
        ("股票列表", init_stock_list),
        ("行业分类", init_industry),
        ("基本面数据", init_fundamentals),
        ("日线数据", init_daily),
        ("基准指数", init_benchmark),
    ]
    for name, fn in steps:
        t0 = time.time()
        n = fn()
        elapsed = time.time() - t0
        logger.info(f"[{name}] {n} rows, {elapsed:.1f}s")
        print(f"  {name}: {n} rows ({elapsed:.1f}s)")
    print("\n初始化完成。运行 check 查看结果:")
    check_data_status()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="量化系统数据初始化")
    parser.add_argument("--check", action="store_true", help="仅检查数据状态")
    parser.add_argument("--stocks", action="store_true", help="仅股票列表")
    parser.add_argument("--industry", action="store_true", help="仅行业分类")
    parser.add_argument("--daily", action="store_true", help="仅日线数据")
    parser.add_argument("--fundamentals", action="store_true", help="仅基本面数据")
    parser.add_argument("--benchmark", action="store_true", help="仅基准指数")
    args = parser.parse_args()

    if args.check:
        check_data_status()
    elif args.stocks:
        init_stock_list()
    elif args.industry:
        init_industry()
    elif args.daily:
        init_daily()
    elif args.fundamentals:
        init_fundamentals()
    elif args.benchmark:
        init_benchmark()
    else:
        init_all()
