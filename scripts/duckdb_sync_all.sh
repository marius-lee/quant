#!/bin/bash
# ── DuckDB 迁移: SQLite → DuckDB 全量同步 + 校验 ──
# v496: 全量同步提速 (executemany → register+INSERT..SELECT, ~5000x):
#       daily 949万 100s (原 2.4h) / valuation 830万 58s (原 2.4h) / refresh 27s (原 96min)
# v498: 预聚合表零消费方, 已删 refresh_preaggregates + DDL; 本脚本幂等 DROP 遗留表
# 用法: bash scripts/duckdb_sync_all.sh
# 幂等: 全量 UPSERT + ON CONFLICT + DROP IF EXISTS, 可重复执行
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

# 3) 预聚合表清理 (v498: 零消费方, DDL 已删; 此处幂等 DROP 遗留历史表)
_preagg_tables = ["daily_ma", "daily_ret", "daily_std", "daily_zscore",
                  "daily_ma_volume", "daily_max", "daily_min", "daily_rank"]
for _t in _preagg_tables:
    m._write(f"DROP TABLE IF EXISTS {_t}")

# 4) 校验
r1 = m.verify_sync("daily")
r2 = m.verify_sync("daily_valuation")
print("daily:", r1["duckdb_rows"], r1["match"], "| valuation:", r2["duckdb_rows"], r2["match"])
EOF
