#!/usr/bin/env bash
# 数据库完整性检查 — 确认历史数据齐全后再扩展因子缓存
set -euo pipefail
cd "$(dirname "$0")/.."

sqlite3 quant/data/market.db << 'EOF'
SELECT 'daily >=2020-01-01:' AS check_item, COUNT(DISTINCT date) AS dates, COUNT(DISTINCT symbol) AS symbols, COUNT(*) AS rows FROM daily WHERE date >= '2020-01-01';
SELECT 'stocks:' AS check_item, COUNT(*) AS total FROM stocks;
SELECT 'benchmark_daily:' AS check_item, COUNT(*) AS rows, MIN(date) AS min_date, MAX(date) AS max_date FROM benchmark_daily;
SELECT 'margin_detail earliest:' AS check_item, MIN(date) AS min_date FROM margin_detail;
SELECT 'lhb_detail earliest:' AS check_item, MIN(trade_date) AS min_date FROM lhb_detail;
SELECT 'financial_income earliest:' AS check_item, MIN(stat_date) AS min_date FROM financial_income;
EOF
