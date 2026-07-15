#!/bin/bash
cd "$(dirname "$0")/.."
# 杀旧进程
lsof -ti:8521 | xargs kill -9 2>/dev/null
sleep 1
# 启动 web
PYTHONPATH=. nohup .venv/bin/python3 web/app.py > /dev/null 2>&1 &
# 启动编排器（补跑盘中任务: signals→execute→monitor→attribution）
mkdir -p logs
PYTHONPATH=. nohup .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler.orchestrator import start
start()
import time
while True:
    time.sleep(60)
" > logs/orchestrator.log 2>&1 &
echo "web :8521 + orchestrator started"
