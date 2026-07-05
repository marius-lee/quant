"""ADR 022: 注册窗口修正因子 + 废弃旧 20d 版本.
- volatility_126d, idio_vol_126d → active (Ch.3.4: 6-12月标准)
- volatility_20d, idio_vol_20d → deprecated (偏离标准126d)
"""
import sqlite3, os

DB = os.path.join(os.path.dirname(__file__), "..", "data", "market.db")
conn = sqlite3.connect(DB)

new_factors = [
    ("volatility_126d", "volatility", "compute_volatility", "Kakushadze & Serur Ch.3.4: 6月=126d 低波动标准 (ADR 022)"),
    ("idio_vol_126d", "volatility", "compute_idiosyncratic_vol", "Ang et al. (2006): 特质波动率 126d 标准 (ADR 022)"),
]

for name, category, func, reason in new_factors:
    existing = conn.execute("SELECT name FROM factor_registry WHERE name=?", (name,)).fetchone()
    if not existing:
        conn.execute("""
            INSERT INTO factor_registry (name, category, compute_fn, status, status_reason, updated_at)
            VALUES (?, ?, ?, 'active', ?, datetime('now', 'localtime'))
        """, (name, category, func, reason))
        print(f"  + {name} registered")
    else:
        conn.execute("""
            UPDATE factor_registry SET status='active', status_reason=?, updated_at=datetime('now', 'localtime')
            WHERE name=?
        """, (reason, name))
        print(f"  ~ {name} reactivated")

# Deprecate 20d versions
for old_name, new_name in [("volatility_20d", "volatility_126d"), ("idio_vol_20d", "idio_vol_126d")]:
    conn.execute("""
        UPDATE factor_registry SET status='deprecated',
        status_reason=?, updated_at=datetime('now', 'localtime')
        WHERE name=?
    """, (f"窗口20d偏离标准126d (ADR 022 Ch.3.4); 替代: {new_name}", old_name))
    print(f"  - {old_name} deprecated → replaced by {new_name}")

conn.commit()

print("\nActive volatility/idio factors:")
for r in conn.execute("SELECT name, status, status_reason FROM factor_registry WHERE category='volatility' AND status='active' ORDER BY name"):
    print(f"  {r[0]:30s}  {r[2]}")

conn.close()
print("\nDone.")
