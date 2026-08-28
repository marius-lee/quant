#!/bin/bash
# Quant Web + Orchestrator 启动脚本
# 用法: ./scripts/restart.sh [legacy|dagster]
#   legacy  — 使用自研 30s 轮询编排器 (默认, 向后兼容)
#   dagster — 使用 Dagster Daemon 编排 (需要 QUANT_ORCHESTRATOR=dagster)

cd "$(dirname "$0")/.."

# ── 模式选择 ──
MODE="${1:-legacy}"
if [[ "$MODE" != "legacy" && "$MODE" != "dagster" ]]; then
    echo "Usage: $0 [legacy|dagster]"
    exit 1
fi
export QUANT_ORCHESTRATOR="$MODE"

# --- 优雅停机 ---
# v573: 同时清理独立 daemon 进程 (`-m quant.scheduler.orchestrator`),
# 否则旧进程持 stale config 常驻重设熔断 flag.
_PATS=("from quant.scheduler import start_all"
       "from quant.scheduler.orchestrator import start"
       "quant.scheduler.orchestrator"
       "dagster-daemon"
       "dagster-webserver"
       "web/app.py")
for pat in "${_PATS[@]}"; do
    pkill -TERM -f "$pat" 2>/dev/null
done
lsof -ti:8521 | xargs kill -TERM 2>/dev/null
lsof -ti:3001 | xargs kill -TERM 2>/dev/null
sleep 5
for pat in "${_PATS[@]}"; do
    pkill -KILL -f "$pat" 2>/dev/null
done
lsof -ti:8521 | xargs kill -KILL 2>/dev/null
lsof -ti:3001 | xargs kill -KILL 2>/dev/null
sleep 1

# 启动 web（无需 Docker，直接用现有 SQLite）
# v578: 显式传入 QUANT_ORCHESTRATOR 防止 web 子进程读不到环境变量
PYTHONPATH=. QUANT_ORCHESTRATOR="$MODE" nohup .venv/bin/python3 web/app.py > /dev/null 2>&1 &
sleep 1

# 启动编排器
mkdir -p logs
if [[ "$MODE" == "dagster" ]]; then
    echo "[$MODE] Starting Dagster-based orchestrator (QUANT_ORCHESTRATOR=dagster)"
    # v575: 统一使用 start_dagster_dev.sh 启动 Dagster 环境
    bash scripts/start_dagster_dev.sh
    echo "web :8521 + dagster started"
else
    echo "[$MODE] Starting legacy orchestrator (30s polling)"
    bash scripts/start_orchestrator.sh legacy
    echo "web :8521 + orchestrator started"
fi
