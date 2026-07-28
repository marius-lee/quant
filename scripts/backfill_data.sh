#!/usr/bin/env bash
# 历史数据回填 — 一次性补齐 lhb / margin / financial 数据
# 用法: bash scripts/backfill_data.sh [from_date] [to_date]
# 耗时: lhb~10min, margin~15min, financial~20min (总计~45min)
set -euo pipefail
cd "$(dirname "$0")/.."

FROM="${1:-2020-01-01}"
TO="${2:-2026-06-30}"

echo "=== 数据回填: $FROM → $TO ==="

PYTHONPATH=. .venv/bin/python3 <<PYEOF
import sys, sqlite3, time, os
from datetime import datetime, timedelta
from quant.utils.logger import get_logger
from quant.config.paths import MARKET_DB

_log = get_logger("backfill")
FROM = "$FROM"
TO = "$TO"
from_dt = datetime.strptime(FROM, "%Y-%m-%d")
to_dt = datetime.strptime(TO, "%Y-%m-%d")

# 1. 龙虎榜 (akshare, 按月批量)
_log.info("=== [1/3] LHB backfill ===")
from quant.data.lhb import sync_range
n = sync_range(from_dt.year, from_dt.month, to_dt.year, to_dt.month)
_log.info(f"  lhb done: {n} rows" if n else "  lhb done: no new data")

# 2. 融资融券 (SSE+SZSE API)
_log.info("=== [2/3] MARGIN backfill ===")
from quant.data.margin import sync_range as margin_sync_range
conn = sqlite3.connect(MARKET_DB, timeout=30)
n = margin_sync_range(FROM, TO, conn=conn)
conn.close()
_log.info(f"  margin done: {n} rows" if n else "  margin done: no new data")

# 3. 基本面 (baostock/同花顺)
_log.info("=== [3/3] FINANCIAL backfill ===")
from quant.data.fundamental import sync_all
conn = sqlite3.connect(MARKET_DB, timeout=30)
# 基本面 sync_all 会拉全量最新, 单次执行即可
result = sync_all(conn, max_fetch=5000)
conn.close()
_log.info(f"  financial done: {result}")

_log.info("=== BACKFILL DONE ===")
PYEOF
