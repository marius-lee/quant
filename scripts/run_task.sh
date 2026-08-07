#!/bin/bash
# === 手动/定时任务执行入口 ===
# 用法:
#   bash scripts/run_task.sh signals       [date]  # 生成信号 (默认今天)
#   bash scripts/run_task.sh execute       [date]  # 执行交易
#   bash scripts/run_task.sh monitor       [date]  # 盘中风控 (单次)
#   bash scripts/run_task.sh attribution   [date]  # 盘后归因 (20:00)
#   bash scripts/run_task.sh factor_cache   [date]  # 增量物化因子 (21:00)
#   bash scripts/run_task.sh evening       [date]  # 晚间链 (19:00, test-v302)
#   bash scripts/run_task.sh weekly                # 周频因子评估
#   bash scripts/run_task.sh daemon                # 启动全天编排器 (08:30-15:30)
#   bash scripts/run_task.sh adj_factor            # 灌复权因子表 (1批/cron小时, B-08)
#   bash scripts/run_task.sh hyperopt      [trials]  # Optuna 超参优化 (§8.2, 默认 200)
set -e
cd "$(dirname "$0")/.."

# 本地凭据 (.env gitignored; JQDATA_USER/PASS 等)
if [ -f .env ]; then set -a; . ./.env; set +a; fi

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
    daily_data)
        echo ">>> TASK: daily_data for $DATE"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler.daily_data import _run
_run('$DATE')
"
        ;;
    factor_cache)
        echo ">>> TASK: factor_cache for $DATE"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler.factor_cache import _run
_run('$DATE', '$DATE')
"
        ;;
    evening)
        echo ">>> TASK: evening chain (daily_data → factor_cache → attribution) for $DATE"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler.evening import _run
_run('$DATE')
"
        ;;
    reconcile)
        echo ">>> TASK: reconcile for $DATE"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler.reconcile import _run
_run('$DATE')
"
        ;;
    adj_factor)
        echo ">>> TASK: adj_factor sync (B-08, 1 call/hour limit)"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.data.store import DataStore
result = DataStore().sync_adj_factor(max_batches=1)
print(result)
"
        ;;
    weekly)
        echo ">>> TASK: weekly factor eval"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler.weekly import _run
_run('$DATE')
"
        ;;
    daemon)
        echo ">>> TASK: all schedulers daemon (orchestrator + weekly thread, v416)"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.scheduler import start_all
start_all()
import time
while True:
    time.sleep(60)
"
        ;;
    hyperopt)
        TRIALS="${2:-200}"
        echo ">>> TASK: hyperopt (Optuna, ${TRIALS} trials, §8.2)"
        PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.excepthook import setup; setup()
from quant.optimizer.hyperopt import run_optimization
run_optimization(n_trials=${TRIALS})
"
        ;;
    *)
        echo "Usage: $0 {signals|execute|monitor|reconcile|evening|attribution|weekly|daemon|hyperopt} [date|trials]"
        echo ""
        echo "  signals      生成当日信号 (08:30)"
        echo "  execute      执行交易 (09:30)"
        echo "  monitor      盘中风控 (09:35-14:55)"
        echo "  reconcile    OMS 日终对账 (15:05)"
        echo "  evening      晚间链: daily_data → factor_cache → attribution (19:00)"
        echo "  attribution  盘后归因 (20:00)"
        echo "  factor_cache 增量因子物化 (21:00)"
        echo "  weekly       周频因子评估刷新 (周六 06:00)"
        echo "  daemon       启动全天编排器"
        echo "  hyperopt     Optuna 超参优化 (§8.2, 第2参数=trials, 默认200)"
        echo ""
        echo "  date 可选, 默认今天 ($DATE)"
        exit 1
        ;;
esac
