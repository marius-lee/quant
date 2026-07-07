"""Stage 1: 数据准备 — 股票池 + 数据范围验证。"""

import sqlite3
import json
from datetime import datetime
from config.loader import get as cfg


def prepare_data(output_json: str = "/tmp/_eval_phase1.json") -> dict:
    """验证股票池和数据范围, 写入 JSON, 返回结果 dict。

    Returns
    -------
    dict with keys: symbols, effective_start, db_max, db_min
    """
    conn = sqlite3.connect("data/market.db")

    # 股票池: 全A, 剔除上市 < 60天 (ST 由 pipeline 层按日期动态过滤)
    stocks = conn.execute("""
        SELECT symbol, name, list_date FROM stocks
        WHERE list_date <= date('now', '-60 days')
    """).fetchall()
    symbols = [r[0] for r in stocks]
    print(f"Phase 1: {len(symbols)} stocks in universe (全A, 上市≥60天)")

    # 有效评估区间
    lookback = cfg("factor.evaluation.lookback", 120)
    backtest_start = cfg("factor.evaluation.backtest_start_date", "2010-01-01")
    effective_start = max(
        backtest_start,
        (datetime.today() - __import__('datetime').timedelta(days=int(lookback * 1.5))).strftime("%Y-%m-%d")
    )

    db_min = conn.execute("SELECT min(date) FROM daily").fetchone()[0]
    db_max = conn.execute("SELECT max(date) FROM daily").fetchone()[0]
    print(f"Phase 1: DB 存储范围 {db_min} → {db_max}")
    print(f"Phase 1: 有效评估区间 {effective_start} → {datetime.today().strftime('%Y-%m-%d')}")
    print(f"Phase 1: pre-2010 数据排除原因 — 股权分置改革前市场结构不成熟 (config backtest_start_date)")

    conn.close()

    result = {"symbols": symbols, "effective_start": effective_start, "db_max": db_max, "db_min": db_min}
    with open(output_json, 'w') as f:
        json.dump(result, f, default=str)
    print("Phase 1 complete")
    return result
