#!/bin/bash
# 修复新增财务因子的 registry 状态 → 全部激活 → 评估IC → 筛选
# 前提: get_financials() 已在 store.py, 因子函数已在 factor/compute.py
set -e
cd "$(dirname "$0")/.."

echo "=== 删除残废行 ==="
.venv/bin/python3 -c "
import sqlite3
conn = sqlite3.connect('quant/data/market.db')
for n in ['roe_reported','roa','debt_ratio','accruals']:
    conn.execute('DELETE FROM factor_registry WHERE name=?', (n,))
conn.commit()
print('Deleted broken rows')
conn.close()
"

echo ""
echo "=== 重新注册（直接 SQL，不跑 build_fin_factors.sh 避免重复插入代码）==="
.venv/bin/python3 -c "
import sqlite3
conn = sqlite3.connect('quant/data/market.db')
for name, cat, fn, src in [
    ('roe_reported', 'profitability', 'compute_roe_reported', 'Fama & French (2015)'),
    ('roa', 'profitability', 'compute_roa', 'Novy-Marx (2013)'),
    ('debt_ratio', 'leverage', 'compute_debt_ratio', 'Penman et al. (2007)'),
    ('accruals', 'quality', 'compute_accruals', 'Sloan (1996)'),
]:
    conn.execute('''INSERT OR IGNORE INTO factor_registry
        (name, category, compute_fn, academic_source, status, direction, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'inactive', 'positive', datetime('now','localtime'), datetime('now','localtime'))''',
        (name, cat, fn, src))
conn.commit()
conn.close()
print('Re-registered 4 factors')
"

echo ""
echo "=== 激活全部 + 评估 + 筛选 + 回测 ==="
bash scripts/reset_eval.sh
