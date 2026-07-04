#!/bin/bash
# 流程: 激活全部因子 → 评估IC → |IC|>0.01 保留
set -e
cd "$(dirname "$0")/.."

echo "=== Step 1: 激活全部因子 ==="
.venv/bin/python3 -c "
import sqlite3
conn = sqlite3.connect('data/market.db')
conn.execute(\"UPDATE factor_registry SET status='active', updated_at=datetime('now','localtime')\")
conn.commit()
cnt = conn.execute(\"SELECT COUNT(*) FROM factor_registry WHERE status='active'\").fetchone()[0]
print(f'Activated: {cnt} factors')
conn.close()
"

echo ""
echo "=== Step 2: 刷新因子 IC ==="
PYTHONPATH=. .venv/bin/python3 -c "
from factor.stats_cache import force_refresh_cache
stats = force_refresh_cache(n_symbols=300)
print()
import sqlite3
conn = sqlite3.connect('data/market.db')
# 新因子 IC
rows = conn.execute(\"SELECT name, ic_mean, ic_ir FROM factor_registry WHERE name IN ('roe_reported','roa','debt_ratio','accruals') ORDER BY name\").fetchall()
print('New financial factor IC:')
for r in rows:
    ic = r[1] or 0
    ir = r[2] or 0
    print(f'  {r[0]:20s} IC={ic:+.4f}  IR={ir:+.2f}')
conn.close()
"

echo ""
echo "=== Step 3: 阈值筛选 (|IC|>=0.02) ==="
.venv/bin/python3 -c "
import sqlite3
conn = sqlite3.connect('data/market.db')
conn.execute(\"UPDATE factor_registry SET status='inactive' WHERE ABS(COALESCE(ic_mean,0)) < 0.02\")
conn.execute(\"UPDATE factor_registry SET status='active' WHERE ABS(COALESCE(ic_mean,0)) >= 0.02\")
conn.commit()
active = conn.execute(\"SELECT name, ic_mean FROM factor_registry WHERE status='active' ORDER BY ABS(ic_mean) DESC\").fetchall()
print(f'Active ({len(active)}):')
for r in active:
    print(f'  {r[0]:30s} IC={r[1]:+.4f}')
conn.close()
"

echo ""
echo "=== Step 4: 回测 ==="
bash scripts/backtest_jq.sh
