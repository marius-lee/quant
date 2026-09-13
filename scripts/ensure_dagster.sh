#!/bin/bash
# 确保 Dagster daemon 运行且 schedules 已启动
# 用法: bash scripts/ensure_dagster.sh

cd "$(dirname "$0")/.."

export DAGSTER_HOME=/tmp/dagster_home
export QUANT_ORCHESTRATOR=dagster
export PYTHONPATH=.

# 1. 检查 daemon 是否运行
if ! pgrep -f "dagster-daemon" > /dev/null; then
    echo "[$(date)] Dagster daemon 未运行，启动中..."
    bash scripts/start_dagster_dev.sh >> logs/dagster_ensure.log 2>&1
    sleep 10
fi

# 2. 启动 schedules（使用 Python API，更可靠）
echo "[$(date)] 启动 schedules..."
.venv/bin/python3 scripts/start_schedules.py >> logs/dagster_ensure.log 2>&1

echo "[$(date)] 完成"
