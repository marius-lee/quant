"""test-v305 backfill verification: 7 window factors + range_20d coverage check.

用法: .venv/bin/python scripts/verify_v305.py
"""
import sqlite3

c = sqlite3.connect("quant/data/factor_cache.db", timeout=60)

latest = c.execute("SELECT MAX(date) FROM factor_values").fetchone()[0]
n = c.execute(
    "SELECT COUNT(DISTINCT factor) FROM factor_values WHERE date=?",
    (latest,)).fetchone()[0]
print(f"latest date: {latest}  factors: {n}/67")

for f in ["abn_turnover", "amihud_250d", "ctr_20d", "dt_streak",
          "hl_volume_20d", "ideal_amplitude", "zt_streak", "range_20d"]:
    rows, dmin, dmax = c.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM factor_values WHERE factor=?",
        (f,)).fetchone()
    print(f"{f:18s} rows={rows:>8}  {dmin} .. {dmax}")

print("log:", c.execute(
    "SELECT date_start, date_end, n_rows, elapsed_sec "
    "FROM materialization_log ORDER BY rowid DESC LIMIT 1").fetchone())
c.close()
