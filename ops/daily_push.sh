#!/bin/bash
# 每日数据同步 + 代码备份
# 23:17 执行 — 收盘后新浪日线已发布

# 1. 拉取日线数据 (传 symbols 强制从最新日期拉, 绕过 gap cutoff=today-2)
echo "[$(date)] 日线同步..."
/Users/mariusto/project/quant/.venv/bin/python -c "
import os, sys
sys.path.insert(0, os.path.expanduser('~/project/quant'))
from data.store import DataStore
ds = DataStore()
conn = ds._connect()
syms = [r[0] for r in conn.execute('SELECT symbol FROM stocks ORDER BY symbol').fetchall()]
conn.close()
total = ds.update_daily(symbols=syms)
print(f'日线同步: {total} 新行')
ds.close()
" 2>&1

# 2. 代码提交+推送
cd /Users/mariusto/project/quant || exit 1

if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
    echo "[$(date)] no changes, skip"
    exit 0
fi

git add --all
if git diff --cached --quiet; then
    echo "[$(date)] nothing to commit"
    exit 0
fi

git commit -m "backup: $(date +%Y-%m-%d) 每日自动备份

Co-Authored-By: Claude <noreply@anthropic.com>"
git push origin main 2>&1

echo "[$(date)] done"
