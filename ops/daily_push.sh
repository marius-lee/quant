#!/bin/bash
# 每日自动提交+推送 — 北极星量化项目
# 只推源码, 跳过数据库/模型/日志

cd /Users/mariusto/project/quant || exit 1

# 无变更则跳过
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
    echo "[$(date)] no changes, skip"
    exit 0
fi

git add --all

# 再次检查暂存区是否有内容
if git diff --cached --quiet; then
    echo "[$(date)] nothing to commit"
    exit 0
fi

git commit -m "backup: $(date +%Y-%m-%d) 每日自动备份

Co-Authored-By: Claude <noreply@anthropic.com>"
git push origin main 2>&1

echo "[$(date)] done"
