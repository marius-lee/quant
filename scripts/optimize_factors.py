"""P0-4: 因子方向修复 + 弱因子退役 + 缓存重算 + 数据一致性

步骤:
  1. 退役 IC < 0.02 的因子 (momentum_10d, reversal_5d, turnover_rev_5d)
  2. 重建 factor_cache.json (方向修复后的新 IC)
  3. 同步 IC 到 factor_registry 表
"""

import sys, os, sqlite3, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
CACHE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "factor_cache.json")

# ── Step 1: 退役弱因子 ──
conn = sqlite3.connect(DB)
weak = ['momentum_10d', 'reversal_5d', 'turnover_rev_5d']
for name in weak:
    conn.execute(
        "UPDATE factor_registry SET status='deprecated', updated_at=datetime('now','localtime') WHERE name=?",
        (name,)
    )
conn.commit()

# 确认
active = conn.execute("SELECT name, ic_mean FROM factor_registry WHERE status='active'").fetchall()
print("Active factors after cleanup:")
for name, ic in active:
    print(f"  {name:20s} IC={ic or 0:+.4f}")
conn.close()

# ── Step 2: 重建缓存 ──
print("\nRebuilding factor_cache.json...")
from factor.stats_cache import force_refresh_cache
stats = force_refresh_cache(n_symbols=500)

# ── Step 3: 同步 IC 到 factor_registry ──
conn = sqlite3.connect(DB)
factor_keys = stats.get('factor_keys', [])
ic_values = stats.get('ic', [])
for key, ic in zip(factor_keys, ic_values):
    conn.execute(
        "UPDATE factor_registry SET ic_mean=?, ic_ir=?, last_evaluated=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE name=?",
        (round(ic, 6), round(abs(ic) / max(0.001, abs(ic)), 2), key)
    )
conn.commit()
conn.close()

# ── Summary ──
print("\nFinal active factors with updated IC:")
conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT name, ic_mean, ic_ir FROM factor_registry WHERE status='active' ORDER BY ABS(ic_mean) DESC"
).fetchall()
for name, ic, ir in rows:
    bar = '█' * max(1, int(abs(ic or 0) * 150))
    print(f"  {name:20s} IC={ic or 0:+.4f} IR={ir or 0:+.2f} {bar}")
conn.close()

print("\nP0-4 complete. Ready for backtest: ./scripts/run_backtest_clean.sh")
