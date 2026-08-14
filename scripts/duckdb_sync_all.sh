#!/bin/bash
# ── DuckDB 迁移: SQLite → DuckDB 全量同步 + 预聚合 + 校验 ──
# v496: 全量同步提速 (executemany → register+INSERT..SELECT, ~5000x):
#       daily 949万 100s (原 2.4h) / valuation 830万 58s (原 2.4h) / refresh 27s (原 96min)
# 用法: bash scripts/duckdb_sync_all.sh
# 幂等: 全量 UPSERT + ON CONFLICT, 可重复执行
cd "$(dirname "$0")/.."
PYTHONPATH=. .venv/bin/python - <<'EOF'
from quant.data.duckdb_store import get_duckdb_proxy

m = get_duckdb_proxy()._duckdb

# 1) 行情 + 估值全量同步 (幂等 UPSERT, v496 后 ~2min)
m.sync_daily_full()
m.sync_table_full(
    "daily_valuation",
    ["symbol", "date", "pe_ttm", "pb", "ps_ttm", "pcf_ttm", "market_cap", "turnover_rate", "source"],
    ["symbol", "date"],
)

# 2) stocks 全量同步 (无 date 列, 每轮全量 5k 行)
m._sync_table(
    "stocks", ["symbol"],
    ["symbol", "name", "market", "list_date", "industry", "list_status", "delist_date"],
    ["list_date", "delist_date"],
)

# 3) 预聚合 (MA/RET/STD/MAX/MIN 6 窗口, 默认近 90 天)
m.refresh_preaggregates()

# 4) 校验
r1 = m.verify_sync("daily")
r2 = m.verify_sync("daily_valuation")
print("daily:", r1["duckdb_rows"], r1["match"], "| valuation:", r2["duckdb_rows"], r2["match"])
EOF
