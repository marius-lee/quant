"""tickflow 换手率回填 — 盘后运行, 更新 daily.turnover"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from quant.data.store import DataStore

s = DataStore()
try:
    n = s.backfill_turnover_quotes()
    print(f"Updated: {n} rows")
finally:
    s.close()
