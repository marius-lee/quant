#!/usr/bin/env python3
"""B-08 一次性迁移: 将 market.db daily 表历史存量数据重拉为前复权(qfq)口径。

背景: B-08 修复前, tushare 写入未复权原始价, 与 tencent/akshare 的 qfq 前复权
混写同一张表, 除权日收益率跳变 (如 -34%), 回测不可复现。代码侧已在
store._fetch_batch_tushare 内用 adj_factor 转 qfq, 但存量历史行仍是旧口径,
且行级无来源标记, 无法逐行甄别 — 唯一干净方案是全量重拉。

本脚本:
  1. 备份 market.db → market.db.bak-YYYYMMDD-HHMMSS
  2. 仅用 tushare (B-08 修复后的 qfq 口径) 全量重写 daily 表
     — 不走多源回退, 避免未复权源 (tickflow) 再次污染
  3. turnover 由 backfill_turnover_quotes 后续补齐 (脚本末尾提示)

用法:
  PYTHONPATH=. .venv/bin/python3 scripts/resync_daily_qfq.py            # 全量
  PYTHONPATH=. .venv/bin/python3 scripts/resync_daily_qfq.py --dry-run  # 只统计
  PYTHONPATH=. .venv/bin/python3 scripts/resync_daily_qfq.py --limit 500  # 前500只
  PYTHONPATH=. .venv/bin/python3 scripts/resync_daily_qfq.py --factors-only  # 只灌因子表

预计耗时: ~5400 股 x 50/批 = 108 批, tushare 200call/min ≈ 30-60 分钟。
(B-08 v2: 因子走本地 adj_factor 表, 不再受 adj_factor 接口 1次/小时限流)
"""
import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quant.config.constants import _require_cfg
from quant.config.paths import MARKET_DB
from quant.data.store import DataStore
from quant.utils.logger import get_logger

logger = get_logger("resync_qfq")


def main() -> int:
    ap = argparse.ArgumentParser(description="B-08: resync daily table to qfq")
    ap.add_argument("--dry-run", action="store_true", help="只统计缺口, 不拉取")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只股票 (试跑)")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份 (不推荐)")
    ap.add_argument("--factors-only", action="store_true",
                    help="只灌本地 adj_factor 因子表 (一次调用, 限流 1次/小时)")
    args = ap.parse_args()

    if args.factors_only:
        store = DataStore()
        if not store.token:
            logger.error("tushare token unavailable (check quant/config/.env) - aborting")
            return 1
        result = store.sync_adj_factor(max_batches=1)
        logger.info(f"sync_adj_factor: {result}")
        if result["remaining"] > 0:
            logger.info(f"remaining {result['remaining']} symbols — "
                        f"rate limit is 1 call/hour, re-run hourly (or add cron)")
        return 0

    conn = sqlite3.connect(MARKET_DB)
    symbols = [r[0] for r in conn.execute(
        "SELECT symbol FROM stocks WHERE symbol NOT LIKE 'BJ%' ORDER BY symbol").fetchall()]
    if args.limit:
        symbols = symbols[:args.limit]
    n_rows = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    logger.info(f"daily: {n_rows} rows, {len(symbols)} symbols to resync")

    if args.dry_run:
        logger.info("dry-run: no changes made")
        conn.close()
        return 0

    if not args.no_backup:
        bak = f"{MARKET_DB}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        logger.info(f"backup: {MARKET_DB} -> {bak}")
        conn.close()
        shutil.copy2(MARKET_DB, bak)
    else:
        conn.close()

    store = DataStore()
    if not store.token:
        logger.error("tushare token unavailable (check quant/config/.env) - aborting")
        return 1

    start = _require_cfg("data.start_date")
    batch_size = _require_cfg("data.batch_size")
    total_new = 0
    t0 = time.time()
    wconn = store._connect()

    for i in range(0, len(symbols), batch_size):
        chunk = symbols[i:i + batch_size]
        # B-08: 直接调 _fetch_batch_tushare (qfq), 绕过 update_daily 的多源回退
        rows = store._fetch_batch_tushare(chunk, start)
        if not rows:
            logger.warning(f"batch {i // batch_size}: tushare returned no rows "
                           f"({len(chunk)} symbols) - keeping old rows, next batch")
            continue
        wconn.executemany(
            """INSERT INTO daily (symbol,date,open,high,low,close,volume,amount,turnover)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, date) DO UPDATE SET
               open=excluded.open, high=excluded.high, low=excluded.low,
               close=excluded.close, volume=excluded.volume, amount=excluded.amount""",
            rows)
        wconn.commit()
        total_new += len(rows)
        done = min(i + batch_size, len(symbols))
        rate = done / max(time.time() - t0, 0.001)
        eta = (len(symbols) - done) / max(rate, 0.001)
        logger.info(f"resync {done}/{len(symbols)} ({done / len(symbols) * 100:.0f}%) "
                    f"{total_new} rows | ETA={eta:.0f}s")

    logger.info(f"resync done: {total_new} rows written in {time.time() - t0:.0f}s")
    logger.info("next: run backfill_turnover_quotes to restore turnover "
                "(daily_sync 任务会自动补)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
