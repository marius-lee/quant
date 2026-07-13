"""交易执行调度器 — 每日 09:30."""
import time as _time, uuid as _uuid
from datetime import time, datetime
from monitor.metrics import metrics as _m
from utils.logger import get_logger
from quant.scheduler._base import _timed_loop

_log = get_logger("quant.scheduler.execute")


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    _log.info(f"[{today}] 09:30 — executing trades")
    t0 = _time.time()

    from pipeline import execute_signals
    from data.trade_repo import TradeRepo

    # 从 daily_signals 表读取今日信号 (持久化, 重启安全)
    sig = TradeRepo().get_latest_signals()
    targets = sig["targets"] if sig and sig["date"] == today else []
    signals_date = sig["date"] if sig else "未知"
    _log.info(f"[{today}] read {len(targets)} targets from daily_signals (generated {signals_date})")

    if not targets:
        _log.error(f"[{today}] 今日无信号，拒绝执行 (no fallback)")
        _m.inc("scheduler.execute.no_targets")
        return

    result = execute_signals(targets, date_str=today)
    elapsed = _time.time() - t0

    orders = result.get("steps", {}).get("execution", {})
    _log.info(f"[{today}] execute done: {orders.get('orders', 0)} orders ({orders.get('buys', 0)} buys, {orders.get('sells', 0)} sells)")
    _log.info(f"[SCHEDULER] {today} | TASK=execute | STATUS=OK | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.execute.ok")


def _loop():
    _timed_loop("execute", time(9, 30), _run, has_multiprocess=True)
