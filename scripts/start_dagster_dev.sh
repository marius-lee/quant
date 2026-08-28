#!/bin/bash
# start_dagster_dev.sh - 一键启动 Dagster 本地开发环境
# 用途: 启动 Dagster Daemon + Webserver + Quant Web (Dagster 模式)
# 版本: v567

set -e

cd "$(dirname "$0")/.."

echo "🚀 启动 Dagster 本地开发环境..."
echo "=========================================="

# 设置环境变量
export DAGSTER_HOME=/tmp/dagster_home
export QUANT_ORCHESTRATOR=dagster
export PYTHONPATH=.

# 创建 Dagster 运行目录
mkdir -p "$DAGSTER_HOME"

echo "1. 启动 Dagster Daemon..."
nohup .venv/bin/dagster-daemon run --module-name quant.orchestrator.dagster_assets \
    > "$DAGSTER_HOME/daemon.log" 2>&1 &
DAEMON_PID=$!
echo "   ✅ Daemon PID: $DAEMON_PID"

echo "2. 启动 Dagster Webserver (http://localhost:3001)..."
nohup .venv/bin/dagster-webserver -h 0.0.0.0 -p 3001 -m quant.orchestrator.dagster_assets \
    > "$DAGSTER_HOME/webserver.log" 2>&1 &
WEBSERVER_PID=$!
echo "   ✅ Webserver PID: $WEBSERVER_PID"

echo "3. 启动 Quant Web (http://localhost:8521)..."
nohup .venv/bin/python3 web/app.py > logs/web.log 2>&1 &
WEB_PID=$!
echo "   ✅ Web PID: $WEB_PID"

echo ""
echo "等待服务就绪 (10 秒)..."
sleep 10

echo ""
echo "✅ Dagster 环境启动完成!"
echo ""
echo "📊 监控面板:"
echo "   - Quant Web: http://localhost:8521"
echo "   - Dagster UI: http://localhost:3001"
echo ""
echo "🛠️ 停止服务:"
echo "   pkill -f dagster-daemon"
echo "   pkill -f dagster-webserver"  
echo "   pkill -f 'web/app.py'"
echo ""
echo "📜 查看日志:"
echo "   tail -f logs/web.log"
echo "   tail -f $DAGSTER_HOME/daemon.log"
echo "   tail -f $DAGSTER_HOME/webserver.log"