#!/bin/bash
# === Dagster Daemon 自动启动脚本 (供 cron/systemd 调用) ===
# 用法: bash scripts/start_dagster_daemon.sh
# 启动 dagster-daemon + dagster-webserver (本地开发模式，无需 Docker)

set -e
cd "$(dirname "$0")/.."

# 设置环境变量
export DAGSTER_HOME=/tmp/dagster_home
export QUANT_ORCHESTRATOR=dagster
export PYTHONPATH=.

# 本地凭据
if [ -f .env ]; then set -a; . ./.env; set +a; fi

# Dagster 配置
DAGSTER_MODULE="quant.orchestrator.dagster_assets"
WEBSERVER_PORT=3001

# 防重复启动: PID 文件
DAEMON_PID_FILE="/tmp/quant_dagster_daemon.pid"
WEBSERVER_PID_FILE="/tmp/quant_dagster_webserver.pid"

# 检查进程是否存活
check_pid() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        local old_pid=$(cat "$pid_file" 2>/dev/null)
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            echo "$old_pid"
            return 0
        fi
    fi
    return 1
}

# 优雅停机旧进程
graceful_kill() {
    local pid_file="$1"
    local name="$2"
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "[$(date)] Stopping $name (pid=$pid)..."
            kill -TERM "$pid" 2>/dev/null
            sleep 3
            if kill -0 "$pid" 2>/dev/null; then
                kill -KILL "$pid" 2>/dev/null
                echo "[$(date)] $name force killed"
            else
                echo "[$(date)] $name stopped gracefully"
            fi
        fi
    fi
}

# 停机旧进程
set +e
graceful_kill "$DAEMON_PID_FILE" "dagster-daemon"
graceful_kill "$WEBSERVER_PID_FILE" "dagster-webserver"
set -e

# 创建运行目录
mkdir -p "$DAGSTER_HOME" logs

# 启动 dagster-daemon
echo "[$(date)] Starting dagster-daemon..."
nohup .venv/bin/dagster-daemon run --module-name "$DAGSTER_MODULE" \
    > "$DAGSTER_HOME/daemon.log" 2>&1 &
DAEMON_PID=$!
echo $DAEMON_PID > "$DAEMON_PID_FILE"
echo "[$(date)] dagster-daemon started (pid=$DAEMON_PID)"

# 等待 daemon 就绪
sleep 2

# 启动 dagster-webserver
echo "[$(date)] Starting dagster-webserver on port $WEBSERVER_PORT..."
nohup .venv/bin/dagster-webserver -h 0.0.0.0 -p $WEBSERVER_PORT -m "$DAGSTER_MODULE" \
    > "$DAGSTER_HOME/webserver.log" 2>&1 &
WEBSERVER_PID=$!
echo $WEBSERVER_PID > "$WEBSERVER_PID_FILE"
echo "[$(date)] dagster-webserver started (pid=$WEBSERVER_PID)"

echo "[$(date)] Dagster services started successfully"
echo "  - Daemon PID: $DAEMON_PID"
echo "  - Webserver PID: $WEBSERVER_PID"
echo "  - Dagster UI: http://localhost:$WEBSERVER_PORT"
echo "  - Quant Web: http://localhost:8521"