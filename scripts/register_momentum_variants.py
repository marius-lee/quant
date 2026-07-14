"""注册动量窗口变体 (ADR 022 Tier 2.1)
Kakushadze & Serur (2018) Ch.3.1: 形成期 T ∈ {3, 6, 9, 12} 个月 = {63, 126, 189, 252} 天
窗口参数由 factor/compute.py _PRICE_FN_MAP 管理, 此处只写 factor_registry 元数据.
"""
import sqlite3
from quant.data.repos._base import DatabaseManager, os

DB = os.path.join(os.path.dirname(__file__), "..", "data", "market.db")
conn = sqlite3.connect(DB)

existing = {r[0] for r in conn.execute("SELECT name FROM factor_registry").fetchall()}

variants = [
    ("momentum_63d", "momentum", "compute_momentum", "Jegadeesh & Titman (1993): T=3月 标准下限 (ADR 022)"),
    ("momentum_126d", "momentum", "compute_momentum", "Jegadeesh & Titman (1993): T=6月 最常用 (ADR 022)"),
    ("momentum_252d", "momentum", "compute_momentum", "Jegadeesh & Titman (1993): T=12月 基准 (ADR 022)"),
]

for name, category, func, reason in variants:
    if name not in existing:
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

# momentum_10d already deleted from registry (ADR 023)

conn.commit()

print("\nActive momentum factors:")
for r in conn.execute("SELECT name, status, status_reason FROM factor_registry WHERE status='active' AND category='momentum' ORDER BY name"):
    print(f"  {r[0]:30s}  {r[2]}")

conn.close()
print("\nDone.")
