#!/bin/bash
# === 手动/定时任务执行入口 ===
# 用法:
#   bash scripts/run_task.sh signals    [date]   # 生成信号 (默认今天)
#   bash scripts/run_task.sh execute    [date]   # 执行交易
#   bash scripts/run_task.sh monitor    [date]   # 盘中风控 (单次)
#   bash scripts/run_task.sh daemon              # 启动全天编排器 (08:30-15:30)
set -e
cd "$(dirname "$0")/.."

TASK="${1:-}"
DATE="${2:-$(date +%Y-%m-%d)}"

case "$TASK" in
    signals)
        echo ">>> TASK: signals for $DATE"
        PYTHONPATH=. .venv/bin/python3 -c "
from utils.excepthook import setup; setup()
from quant.scheduler.signals import _run
_run('$DATE')
"
        ;;
    execute)
        echo ">>> TASK: execute for $DATE"
        PYTHONPATH=. .venv/bin/python3 -c "
from utils.excepthook import setup; setup()
from quant.scheduler.execute import _run
_run('$DATE')
"
        ;;
    monitor)
        echo ">>> TASK: monitor for $DATE"
        PYTHONPATH=. .venv/bin/python3 -c "
from utils.excepthook import setup; setup()
from quant.scheduler.monitor import _run_continuous
_run_continuous('$DATE')
"
        ;;
    daemon)
        echo ">>> TASK: orchestrator daemon (08:30-15:30 daily)"
        PYTHONPATH=. .venv/bin/python3 -c "
from utils.excepthook import setup; setup()
from quant.scheduler.orchestrator import start
start()
import time
# Keep main thread alive
while True:
    time.sleep(60)
"
        ;;
    *)
        echo "Usage: $0 {signals|execute|monitor|daemon} [date]"
        echo ""
        echo "  signals    生成当日信号 (08:30)"
        echo "  execute    执行交易 (09:30)"
        echo "  monitor    盘中风控 (09:35-14:55)"
        echo "  daemon     启动全天编排器"
        echo ""
        echo "  date 可选, 默认今天 ($DATE)"
        exit 1
        ;;
esac
