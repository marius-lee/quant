#!/bin/bash
# === Dagster 启动脚本 (本地开发模式) ===
# v577 fix: 共享持久化 gRPC code server 架构
#
# 架构:
#   1. dagster-grpc (持久化): 加载 definitions, 所有服务共享
#   2. dagster-daemon: 连接到 gRPC server, 管理调度/Sensor/资产
#   3. dagster-webserver: 连接到 gRPC server, 提供 UI
#
# 好处: 只有一个 gRPC server 进程, 不再有 heartbeat 冲突

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# 环境变量
export DAGSTER_HOME="${DAGSTER_HOME:-/tmp/dagster_home}"
export QUANT_ORCHESTRATOR=dagster
export PYTHONPATH="$PROJECT_ROOT"
DAGSTER_MODULE="quant.orchestrator.dagster_assets"
WEBSERVER_PORT="${WEBSERVER_PORT:-3001}"
GRPC_SOCKET="${DAGSTER_HOME}/dagster_grpc.sock"

# PID 文件
GRPC_PID_FILE="/tmp/quant_dagster_grpc.pid"
DAEMON_PID_FILE="/tmp/quant_dagster_daemon.pid"
WEBSERVER_PID_FILE="/tmp/quant_dagster_webserver.pid"

log_info()  { echo "[$(date '+%H:%M:%S')] [INFO]  $1"; }
log_error() { echo "[$(date '+%H:%M:%S')] [ERROR] $1" >&2; }

# ── 1. 清理所有残留进程 ──────────────────────────────

log_info "=== 清理残留进程 ==="
for pattern in "dagster-grpc" "dagster-daemon" "dagster-webserver" "dagster.*grpc"; do
    for pid in $(pgrep -f "$pattern" 2>/dev/null); do
        log_info "Killing $pattern (pid=$pid)..."
        kill -9 "$pid" 2>/dev/null || true
    done
done
sleep 2

# 清理端口
for port in 3001 3333; do
    for pid in $(lsof -ti tcp:$port 2>/dev/null); do
        log_info "Killing process on port $port (pid=$pid)..."
        kill -9 "$pid" 2>/dev/null || true
    done
done
sleep 2

# 清理旧 socket
rm -f "$GRPC_SOCKET"

# 初始化目录
mkdir -p "$DAGSTER_HOME" logs
touch "$DAGSTER_HOME/dagster.yaml"

# 加载 .env
if [ -f "$PROJECT_ROOT/.env" ]; then set -a; . "$PROJECT_ROOT/.env"; set +a; fi

# ── 2. 验证 definitions ────────────────────────────────

log_info "验证 definitions..."
"$PROJECT_ROOT/.venv/bin/python3" -c "
import sys; sys.path.insert(0, '$PROJECT_ROOT')
from quant.orchestrator.dagster_assets import get_definitions
defs = get_definitions()
print(f'  {len(defs.assets)} assets, {len(defs.jobs)} jobs, {len(defs.schedules)} schedules, {len(defs.sensors)} sensors')
" || { log_error "Definitions 加载失败"; exit 1; }

# ── 3. 启动 gRPC code server (共享, 持久化) ──────────

log_info "启动共享 gRPC code server..."
nohup "$PROJECT_ROOT/.venv/bin/dagster" api grpc \
    --lazy-load-user-code \
    --socket "$GRPC_SOCKET" \
    --module-name "$DAGSTER_MODULE" \
    > "$DAGSTER_HOME/grpc.log" 2>&1 &
GRPC_PID=$!
echo "$GRPC_PID" > "$GRPC_PID_FILE"
log_info "gRPC server (pid=$GRPC_PID, socket=$GRPC_SOCKET)"

# 等待 socket 可用
for i in $(seq 1 20); do
    if [ -S "$GRPC_SOCKET" ]; then
        log_info "gRPC socket 就绪 ✅"
        break
    fi
    if [ $i -eq 20 ]; then
        log_error "gRPC socket 超时未就绪"
        exit 1
    fi
    sleep 1
done

# ── 4. 启动 dagster-daemon ────────────────────────────

log_info "启动 dagster-daemon..."
nohup "$PROJECT_ROOT/.venv/bin/dagster-daemon" run \
    --grpc-socket "$GRPC_SOCKET" \
    > "$DAGSTER_HOME/daemon.log" 2>&1 &
DAEMON_PID=$!
echo "$DAEMON_PID" > "$DAEMON_PID_FILE"
log_info "dagster-daemon (pid=$DAEMON_PID)"

# ── 4.5 启动所有 schedules ──────────────────────────
# v577 fix: Dagster daemon 启动不会自动启动 schedules，
# 必须在 daemon 就绪后显式调用 dagster schedule start --start-all，
# 否则 get_schedules_to_be_launched 跳过所有 is_running=False 的 schedule
# (DEFAULT_MAX_CATCHUP_RUNS=5, 不及时启动会漏触发)
log_info "等待 daemon 就绪 (5s)..."
sleep 5
log_info "启动所有 schedules..."
DAGSTER_HOME="$DAGSTER_HOME" "$PROJECT_ROOT/.venv/bin/dagster" schedule start \
    --start-all \
    -m "$DAGSTER_MODULE" \
    > "$DAGSTER_HOME/schedule_start.log" 2>&1 &
SCHEDULE_START_PID=$!
wait "$SCHEDULE_START_PID"
if grep -q "Started all schedules" "$DAGSTER_HOME/schedule_start.log" 2>/dev/null; then
    log_info "所有 schedules 已启动 ✅"
else
    log_warn "schedules 启动可能有警告: $(cat "$DAGSTER_HOME/schedule_start.log" | tail -3)"
fi

# ── 5. 启动 dagster-webserver ─────────────────────────

log_info "启动 dagster-webserver (port $WEBSERVER_PORT)..."
nohup "$PROJECT_ROOT/.venv/bin/dagster-webserver" \
    --grpc-socket "$GRPC_SOCKET" \
    -p "$WEBSERVER_PORT" \
    > "$DAGSTER_HOME/webserver.log" 2>&1 &
WEBSERVER_PID=$!
echo "$WEBSERVER_PID" > "$WEBSERVER_PID_FILE"
log_info "dagster-webserver (pid=$WEBSERVER_PID)"

# 等待 webserver 就绪
for i in $(seq 1 15); do
    if lsof -i tcp:$WEBSERVER_PORT 2>/dev/null | grep -q LISTEN; then
        log_info "Webserver listening on port $WEBSERVER_PORT ✅"
        break
    fi
    if [ $i -eq 15 ]; then
        log_error "Webserver 未就绪"
    fi
    sleep 1
done

# ── 6. 最终验证 ───────────────────────────────────────

log_info "=== 进程状态 ==="
ps aux | grep -E "dagster" | grep -v grep | awk '{print $2, $11, $12}' | while read pid bin arg; do
    case "$arg" in
        *grpc*)   echo "  gRPC  $pid $arg" ;;
        *daemon*) echo "  Daemon $pid $arg" ;;
        *webserver*) echo "  Web    $pid $arg" ;;
        *)        echo "  Other  $pid $arg" ;;
    esac
done

echo ""
log_info "=== 启动完成 ==="
log_info "  gRPC:      pid=$GRPC_PID (socket)"
log_info "  Daemon:   pid=$DAEMON_PID"
log_info "  Webserver: pid=$WEBSERVER_PID (port $WEBSERVER_PORT)"
log_info "  DAGSTER_HOME=$DAGSTER_HOME"
log_info "  UI: http://localhost:$WEBSERVER_PORT"
echo ""
log_info "停止: bash $0 stop"
