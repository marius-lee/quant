"""手动任务执行器 — signals / execute / cleanup."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "quant"))
from quant.utils.excepthook import setup; setup()

from quant.utils.logger import get_logger

_log = get_logger("scripts.run_task")

def main():
    if len(sys.argv) < 2:
        _log.error("Usage: scripts/run_task.py <signals|execute|cleanup> [YYYY-MM-DD]")
        sys.exit(1)

    task = sys.argv[1]
    date = sys.argv[2] if len(sys.argv) > 2 else None

    if task == "signals":
        from quant.pipeline import generate_signals
        result = generate_signals(date_str=date, skip_pull=True)
        _log.info(f"signals done: {len(result.get('target_positions', []))} targets")
    elif task == "execute":
        from quant.pipeline import execute_signals
        from quant.data.trade_repo import TradeRepo
        sig = TradeRepo().get_latest_signals()
        targets = sig["targets"] if sig else []
        _log.info(f"read {len(targets)} targets from daily_signals")
        if not targets:
            _log.error("No signals to execute (no fallback)")
            sys.exit(1)
        result = execute_signals(targets, date_str=date or sig["date"])
        _log.info(f"execute done: {result.get('steps', {}).get('execution', {}).get('orders', 0)} orders")
    elif task == "cleanup":
        from quant.data.store import DataStore
        store = DataStore()
        n = store.cleanup_old_data()
        _log.info(f"cleanup: {n} old rows removed")
        store.close()
    else:
        _log.error(f"Unknown task: {task}")
        sys.exit(1)

if __name__ == "__main__":
    main()
