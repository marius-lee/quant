"""三阶段交易日调度器 — 盘前信号 → 开盘执行 → 盘后归因。

三阶段:
  Phase 1: 08:30 — generate_signals() 产出目标持仓
  Phase 2: 09:30 — execute_signals() 执行模拟交易
  Phase 3: 15:30 — brinson_attribution() 归因分析

仅在 web app 启动时作为 daemon 线程启动，不独立运行。
调度逻辑: 每 30s 检查一次，达到目标时间且今日未运行过则触发。
"""
import threading, time as _time
from datetime import datetime, time

from utils.logger import get_logger

logger = get_logger("quant.scheduler")

# 三阶段时间 (24h)
PHASE_TIMES = {
    1: time(8, 30),   # 盘前信号
    2: time(9, 30),   # 开盘执行
    3: time(15, 30),  # 盘后归因
}


def start_scheduler():
    """启动调度线程 (daemon). 从 web app 的 _warm_factor_cache 中调用."""
    from execution.calendar import is_trading_day

    def _loop():
        ran_phases = {}    # {today: {phase_num: bool}}
        today = None

        logger.info("scheduler started — three-phase mode (08:30 signals, 09:30 execute, 15:30 attribution)")

        while True:
            now = datetime.now()
            current_day = now.strftime("%Y-%m-%d")

            # 跨天重置
            if current_day != today:
                today = current_day
                ran_phases = {today: {}}

            # 非交易日跳过
            if not is_trading_day():
                _time.sleep(30)
                continue

            hhmm = time(now.hour, now.minute)
            phases_today = ran_phases.setdefault(today, {})

            # Phase 1: 08:30 盘前信号生成
            if hhmm >= PHASE_TIMES[1] and not phases_today.get(1):
                _run_phase1(today)
                phases_today[1] = True

            # Phase 2: 09:30 开盘执行
            if hhmm >= PHASE_TIMES[2] and not phases_today.get(2):
                _run_phase2(today)
                phases_today[2] = True

            # Phase 3: 15:30 盘后归因 (跳过 15:45 后, 可能已过归因窗口)
            if hhmm >= PHASE_TIMES[3] and not phases_today.get(3):
                if hhmm <= time(15, 45):
                    _run_phase3(today)
                    phases_today[3] = True
                else:
                    logger.info(f"[{today}] Phase 3 skipped (past 15:45), waiting for next day")
                    phases_today[3] = True  # 标记已处理, 不再尝试

            _time.sleep(30)

    t = threading.Thread(target=_loop, daemon=True, name="quant-scheduler")
    t.start()
    logger.info("scheduler thread launched")


def _run_phase1(today: str):
    """Phase 1: 盘前信号生成."""
    import uuid, time
    from monitor.metrics import metrics as _m
    tid = uuid.uuid4().hex[:12]

    logger.info(f"[{today}] 08:30 — Phase 1: generating signals")
    t0 = time.time()

    try:
        from pipeline import generate_signals
        result = generate_signals(date_str=today)
        targets = result.get("target_positions", [])
        elapsed = time.time() - t0
        logger.info(f"[{today}] Phase 1 done: {len(targets)} target positions ({elapsed:.1f}s)", extra={"trace_id": tid})
        logger.info(f"[SCHEDULER] {today} | PHASE=1 | STATUS=OK | targets={len(targets)} | elapsed={elapsed:.1f}s", extra={"trace_id": tid})
        _m.inc("scheduler.phase1.ok")
    except Exception as e:
        logger.error(f"[{today}] Phase 1 failed: {e}", extra={"trace_id": tid})
        _m.inc("scheduler.phase1.error")


def _run_phase2(today: str):
    """Phase 2: 开盘执行."""
    import uuid, time
    from monitor.metrics import metrics as _m
    tid = uuid.uuid4().hex[:12]

    logger.info(f"[{today}] 09:30 — Phase 2: executing trades")
    t0 = time.time()

    try:
        from pipeline import execute_signals, generate_signals
        # 重新获取信号 (保底: 如果 Phase 1 失败, 这里重新生成)
        signals = generate_signals(date_str=today)
        targets = signals.get("target_positions", [])

        if not targets:
            logger.info(f"[{today}] Phase 2: no target positions, skipping execution")
            return

        exec_result = execute_signals(targets, date_str=today)
        elapsed = time.time() - t0
        logger.info(f"[SCHEDULER] {today} | PHASE=2 | STATUS=OK | elapsed={elapsed:.1f}s", extra={"trace_id": tid})
        _m.inc("scheduler.phase2.ok")
    except Exception as e:
        logger.error(f"[{today}] Phase 2 failed: {e}", extra={"trace_id": tid})
        _m.inc("scheduler.phase2.error")


def _run_phase3(today: str):
    """Phase 3: 盘后归因."""
    import uuid, time
    from monitor.metrics import metrics as _m
    tid = uuid.uuid4().hex[:12]

    logger.info(f"[{today}] 15:30 — Phase 3: attribution")
    t0 = time.time()

    try:
        from monitor.attribution import brinson_attribution
        from execution.engine import ExecutionEngine
        engine = ExecutionEngine()
        positions = engine.get_positions(strategy="quant")

        if positions:
            attribution = brinson_attribution(positions, date=today, benchmark="000300")
            logger.info(f"[{today}] Phase 3 done: {attribution.get('summary', 'N/A')}", extra={"trace_id": tid})
        else:
            logger.info(f"[{today}] Phase 3: no positions, skipped attribution")

        elapsed = time.time() - t0
        logger.info(f"[SCHEDULER] {today} | PHASE=3 | STATUS=OK | elapsed={elapsed:.1f}s", extra={"trace_id": tid})
        _m.inc("scheduler.phase3.ok")
    except Exception as e:
        logger.error(f"[{today}] Phase 3 failed: {e}", extra={"trace_id": tid})
        _m.inc("scheduler.phase3.error")
