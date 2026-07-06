#!/bin/bash
# 重启 quant 调度器 + Web 应用（加载最新代码）
set -e

cd /Users/mariusto/project/quant

echo "=== 停止旧进程 ==="
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.quant.scheduler.plist 2>/dev/null || echo "  scheduler 未运行"
pkill -f "python.*web/app.py" 2>/dev/null || echo "  web app 未运行"
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null && echo "  pycache cleared"
sleep 2

echo "=== 启动 Web 应用 ==="
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quant.webapp.plist 2>/dev/null ||   launchctl kickstart gui/$(id -u)/com.quant.webapp 2>/dev/null || echo "  webapp already loaded"
sleep 2

echo "=== 加载调度器 ==="
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quant.scheduler.plist

echo "=== 验证 ==="
sleep 1
launchctl list | grep quant
echo ""
echo "重启完成。scheduler 将按三时段自动执行。"
