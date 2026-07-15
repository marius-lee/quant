#!/bin/bash
cd "$(dirname "$0")/.."
# 杀旧进程
lsof -ti:8521 | xargs kill -9 2>/dev/null
sleep 1
# 启动
PYTHONPATH=. nohup .venv/bin/python3 web/app.py > /dev/null 2>&1 &
echo "web started on :8521"
