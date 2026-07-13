"""Stage 1: 数据准备 — 股票池 + 数据范围验证。"""

import sqlite3
from datetime import datetime
from config.constants import _require_cfg
import traceback
from utils.logger import get_logger, set_trace_id


def prepare_data(output_json: str = "/tmp/_eval_phase1.json") -> dict:
    import uuid; tid = uuid.uuid4().hex[:12]; set_trace_id(tid)
    logger = get_logger("evaluation.phase1")
    t0 = __import__("time").monotonic()
    logger.info(f"Phase 1 [{tid}] start — data preparation")
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
    logger.info(f"Phase 1 {len(symbols)} stocks in universe (全A, 上市≥60天)")

    # 有效评估区间
    lookback = _require_cfg("factor.evaluation.lookback")
    backtest_start = _require_cfg("factor.evaluation.backtest_start_date")
    effective_start = max(
        backtest_start,
        (datetime.today() - __import__('datetime').timedelta(days=int(lookback * 1.5))).strftime("%Y-%m-%d")
    )

    db_min = conn.execute("SELECT min(date) FROM daily").fetchone()[0]
    db_max = conn.execute("SELECT max(date) FROM daily").fetchone()[0]
    logger.info(f"Phase 1 DB 存储范围 {db_min} → {db_max}")
    logger.info(f"Phase 1 有效评估区间 {effective_start} → {datetime.today().strftime('%Y-%m-%d')}")
    logger.info("Phase 1 pre-2010 数据排除原因 — 股权分置改革前市场结构不成熟 (config backtest_start_date)")

    conn.close()

    db_status = "ok" if db_min and db_max and db_max >= effective_start else "degraded"
    result = {"db_status": db_status, "n_stocks": len(symbols),
              "effective_start": effective_start, "db_max": db_max, "db_min": db_min}

    # 写入 evaluation_runs (ADR 028: DB 替代临时文件)
    try:
        from evaluation.run_store import save_phase
        save_phase("phase1", result)
        logger.info("Phase 1 saved to evaluation_runs")
    except Exception as _e:
        logger.error("Phase 1 save_phase traceback: %s", _e, exc_info=True)

    logger.info(f"Phase 1 complete ({__import__('time').monotonic()-t0:.1f}s)")
    return result
