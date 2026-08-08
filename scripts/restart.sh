#!/bin/bash
cd "$(dirname "$0")/.."
# ── 优雅停机 (v424): 先 TERM 给 cleanup 时间, 再 KILL 兜底 ──
# 覆盖两代入口: scheduler.start_all + orchestrator.start (旧进程也要杀得掉)
_PATS=("from quant.scheduler import start_all" "from quant.scheduler.orchestrator import start")
for pat in "${_PATS[@]}"; do
  pkill -TERM -f "$pat" 2>/dev/null
done
lsof -ti:8521 | xargs kill -TERM 2>/dev/null
sleep 5  # 宽限: 让 task_log.finish / 优雅退出执行完毕
for pat in "${_PATS[@]}"; do
  pkill -KILL -f "$pat" 2>/dev/null
done
lsof -ti:8521 | xargs kill -KILL 2>/dev/null
sleep 1
# 启动 web
PYTHONPATH=. nohup .venv/bin/python3 web/app.py > /dev/null 2>&1 &
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