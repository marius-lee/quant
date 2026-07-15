#!/bin/bash
# === 手动/定时任务执行入口 ===
# 用法:
#   bash scripts/run_task.sh signals       [date]  # 生成信号 (默认今天)
#   bash scripts/run_task.sh execute       [date]  # 执行交易
#   bash scripts/run_task.sh monitor       [date]  # 盘中风控 (单次)
#   bash scripts/run_task.sh attribution   [date]  # 盘后归因 (15:30)
#   bash scripts/run_task.sh weekly                # 周频因子评估
#   bash scripts/run_task.sh daemon                # 启动全天编排器 (08:30-15:30)
set -e
cd "$(dirname "$0")/.."

TASK="${1:-}"
DATE="${2:-$(date +%Y-%m-%d)}"

case "$TASK" in
    signals)
        echo ">>> TASK: signals for $DATE"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler.signals import _run
_run('$DATE')
"
        ;;
    execute)
        echo ">>> TASK: execute for $DATE"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler.execute import _run
_run('$DATE')
"
        ;;
    monitor)
        echo ">>> TASK: monitor for $DATE"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler.monitor import _run_continuous
_run_continuous('$DATE')
"
        ;;
    attribution)
        echo ">>> TASK: attribution for $DATE"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler.attribution import _run
_run('$DATE')
"
        ;;
    weekly)
        echo ">>> TASK: weekly factor eval"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler.weekly import _run
_run()
"
        ;;
    daemon)
        echo ">>> TASK: orchestrator daemon (08:30-15:30 daily)"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler.orchestrator import start
start()
import time
while true:
    time.sleep(60)
"
        ;;
    *)
        echo "Usage: $0 {signals|execute|monitor|attribution|weekly|daemon} [date]"
        echo ""
        echo "  signals      生成当日信号 (08:30)"
        echo "  execute      执行交易 (09:30)"
        echo "  monitor      盘中风控 (09:35-14:55)"
        echo "  attribution  盘后归因 (15:30)"
        echo "  weekly       周频因子评估刷新 (周六 06:00)"
        echo "  daemon       启动全天编排器"
        echo ""
        echo "  date 可选, 默认今天 ($DATE)"
        exit 1
        ;;
esac
