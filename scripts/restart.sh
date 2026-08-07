#!/bin/bash
cd "$(dirname "$0")/.."
# 杀旧进程
lsof -ti:8521 | xargs kill -9 2>/dev/null
sleep 1
# 启动 web
PYTHONPATH=. nohup .venv/bin/python3 web/app.py > /dev/null 2>&1 &
# 杀旧 orchestrator 进程（避免内存泄漏: 每次重启必须杀旧进程）
# v416: 入口改为 scheduler.start_all (orchestrator + weekly 双线程)
pkill -f "from quant.scheduler import start_all" 2>/dev/null
sleep 1
# 启动编排器（补跑盘中任务: signals→execute→monitor→attribution）+ 周频评估线程
mkdir -p logs
PYTHONPATH=. nohup .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler import start_all
start_all()
import time
while True:
    time.sleep(60)
" > logs/orchestrator.log 2>&1 &
echo "web :8521 + orchestrator started"
