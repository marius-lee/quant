"""手动任务执行器 — 与定时调度器等价, 可独立运行.
用法:
  PYTHONPATH=. .venv/bin/python3 scripts/run_task.py signals  [YYYY-MM-DD]
  PYTHONPATH=. .venv/bin/python3 scripts/run_task.py execute  [YYYY-MM-DD]
  PYTHONPATH=. .venv/bin/python3 scripts/run_task.py cleanup  [YYYY-MM-DD]
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date as _date

today = sys.argv[2] if len(sys.argv) > 2 else _date.today().strftime("%Y-%m-%d")

if len(sys.argv) < 2:
    print("Usage: run_task.py <signals|execute|cleanup> [date]")
    sys.exit(1)

task = sys.argv[1]

if task == "signals":
    from quant.scheduler.signals import _run
    _run(today)

elif task == "execute":
    from quant.scheduler.execute import _run
    _run(today)

elif task == "cleanup":
    from quant.scheduler.attribution import _run
    _run(today)

else:
    print(f"Unknown task: {task}. Choose: signals, execute, cleanup")
    sys.exit(1)
