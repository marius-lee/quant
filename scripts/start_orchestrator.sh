#!/bin/bash
# === Orchestrator 自动启动脚本 (供 cron/systemd 调用) ===
# 用法: bash scripts/start_orchestrator.sh [legacy|dagster]
#   legacy  — 使用自研 30s 轮询编排器 (默认)
#   dagster — 使用 Dagster Daemon 编排

set -e
cd "$(dirname "$0")/.."

MODE="${1:-legacy}"
export QUANT_ORCHESTRATOR="$MODE"

# 本地凭据
if [ -f .env ]; then set -a; . ./.env; set +a; fi

# 防重复启动: 若已有 orchestrator 进程存活则退出
# 使用 PID 文件避免误杀无关进程
PID_FILE="/tmp/quant_orchestrator.pid"
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[$(date)] orchestrator already running (pid=$OLD_PID), skip"
        exit 0
    fi
fi

# 优雅停机旧进程
set +e
pkill -TERM -f "quant.scheduler.orchestrator" 2>/dev/null
pkill -TERM -f "from quant.scheduler import start_all" 2>/dev/null
sleep 2
pkill -KILL -f "quant.scheduler.orchestrator" 2>/dev/null
pkill -KILL -f "from quant.scheduler import start_all" 2>/dev/null
set -e

mkdir -p logs

if [[ "$MODE" == "dagster" ]]; then
    echo "[$(date)] Starting Dagster-based orchestrator"
    # Dagster 模式: Web 服务连接到 Dagster Webserver
    # Dagster Daemon/Webserver 需通过 Docker 或独立进程运行
    echo "Dagster webserver: http://localhost:3001"
    echo "Dagster daemon: running in background"
    echo "启动 Dagster 服务: bash scripts/start_dagster.sh start dev"
else
    echo "[$(date)] Starting legacy orchestrator (30s polling)"
    PYTHONPATH=. nohup .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler import start_all
start_all()
import time
while True:
    time.sleep(60)
" > logs/orchestrator.log 2>&1 &
    ORCH_PID=$!
    echo $ORCH_PID > "$PID_FILE"
    echo "[$(date)] orchestrator started (pid=$ORCH_PID)"
fi