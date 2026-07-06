"""量化系统调度器 — 三阶段自动化。

阶段一 (08:30): 盘前信号生成 — generate_signals() → 目标持仓
阶段二 (09:30): 开盘执行 — execute_signals() → 模拟成交
阶段三 (15:30): 盘后归因 — daily_sync + 绩效报告

启动:
  PYTHONPATH=. .venv/bin/python3 scheduler.py &
"""

import time, sys
from datetime import datetime, timedelta
from utils.logger import get_logger
from execution.calendar import is_trading_day, is_market_open

logger = get_logger("scheduler")
MINUTE = 60
_HOUR = 3600


def wait_until(target_hour: int, target_minute: int = 0):
    """休眠至下一个目标时间 (HH:MM)。提前 60s 开始轮询，避免休眠过点。"""
    while True:
        now = datetime.now()
        target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        if now >= target:
            return
        wait_sec = (target - now).total_seconds()
        sleep_time = max(1, min(wait_sec - 60, MINUTE))
        time.sleep(sleep_time)


def phase1_generate_signals(date_str: str):
    """阶段一: 08:30 — 用 T-1 数据生成今日目标持仓。"""
    logger.info(f"[{date_str}] 08:30 — Phase 1: generating signals")
    from pipeline import generate_signals
    try:
        result = generate_signals(date_str)
        n_targets = len(result.get("target_positions", []))
        logger.info(f"[{date_str}] Phase 1 done: {n_targets} target positions "
                     f"({result.get('elapsed_sec', 0):.1f}s)")
        logger.info("[SCHEDULER] %s | PHASE=1 | STATUS=OK | targets=%d | elapsed=%.1fs",
                     date_str, n_targets, result.get('elapsed_sec', 0))
        return result
    except Exception as e:
        logger.error(f"[{date_str}] Phase 1 failed: {e}")
        logger.error("[SCHEDULER] %s | PHASE=1 | STATUS=FAIL | error=%s",
                      date_str, str(e)[:120])
        return None


def phase2_execute_signals(date_str: str, target_positions: list):
    """阶段二: 09:30 — 用开盘价执行调仓。"""
    logger.info(f"[{date_str}] 09:30 — Phase 2: executing trades")
    from pipeline import execute_signals
    try:
        result = execute_signals(target_positions, date_str)
        orders = result.get("steps", {}).get("execution", {}).get("orders", 0)
        logger.info(f"[{date_str}] Phase 2 done: {orders} orders "
                     f"({result.get('elapsed_sec', 0):.1f}s)")
        logger.info("[SCHEDULER] %s | PHASE=2 | STATUS=OK | orders=%d | elapsed=%.1fs",
                     date_str, orders, result.get('elapsed_sec', 0))
        return result
    except Exception as e:
        logger.error(f"[{date_str}] Phase 2 failed: {e}")
        logger.error("[SCHEDULER] %s | PHASE=2 | STATUS=FAIL | error=%s",
                      date_str, str(e)[:120])
        return None


def phase3_daily_sync_and_report(date_str: str):
    """阶段三: 15:30 — 拉取今日完整数据 + 生成日度报告。"""
    logger.info(f"[{date_str}] 15:30 — Phase 3: post-market sync + report")
    
    # Step A: 数据同步
    from daily_sync import run as sync_run
    try:
        sync_results = sync_run(date_str)
        logger.info(f"[{date_str}] daily_sync: {sync_results}")
    except Exception as e:
        logger.error(f"[{date_str}] daily_sync failed: {e}")

    # Step B: 绩效归因报告 (不执行新交易, 只是更新估值+生成报表)
    try:
        from execution.engine import ExecutionEngine
        from monitor.report import generate_report, push_to_web
        engine = ExecutionEngine()
        positions = engine.get_positions("quant")
        trades = engine.get_trades("quant", limit=50)
        total_wealth = engine.get_capital("quant")
        cash_balance = engine.get_cash("quant")
        from data.trade_repo import TradeRepo; seed = TradeRepo().get_initial_capital("quant") or 5000
        report = generate_report(
            date_str, cash_balance, positions, trades,
            pnl_total=total_wealth - seed,
            initial_capital=seed,
        )
        push_to_web(report)
        logger.info(f"[{date_str}] Phase 3 report: wealth=Y{report['capital']['total_wealth']:,.2f} "
                     f"return={report['metrics']['total_return_pct']}%")
        logger.info("[SCHEDULER] %s | PHASE=3 | STATUS=OK | wealth=%.0f | return=%.1f%%",
                     date_str, report['capital']['total_wealth'], report['metrics']['total_return_pct'])
    except Exception as e:
        logger.error(f"[{date_str}] Phase 3 report failed: {e}")
        logger.error("[SCHEDULER] %s | PHASE=3 | STATUS=FAIL | error=%s",
                      date_str, str(e)[:120])


def run_loop():
    """主循环: 等待每个交易日 08:30 → 09:30 → 15:30 依次执行。"""
    logger.info("scheduler started — three-phase mode (08:30 signals, 09:30 execute, 15:30 attribution)")
    
    # 存储阶段一产出的目标持仓, 供阶段二使用
    pending_targets: list = []

    while True:
        try:
            dt = datetime.now()
            date_str = dt.strftime("%Y-%m-%d")
            now_t = dt.time()
            skip_morning = now_t >= datetime.strptime("09:30", "%H:%M").time()
            skip_phase3 = now_t >= datetime.strptime("15:45", "%H:%M").time()

            if not is_trading_day(dt.date()):
                time.sleep(MINUTE * 5)  # 非交易日: 5分钟轮询
                continue

            # ── Phase 1: 08:30 盘前信号 ──
            if skip_morning:
                logger.info(f"[{date_str}] Phase 1+2 skipped (past 09:30)")
                pending_targets = []
            else:
                wait_until(8, 30)
                signals = phase1_generate_signals(date_str)
                if signals and "target_positions" in signals:
                    pending_targets = signals["target_positions"]
                else:
                    pending_targets = []

            # ── Phase 2: 09:30 开盘执行 ──
            if not skip_morning and pending_targets:
                wait_until(9, 30)
                phase2_execute_signals(date_str, pending_targets)
                pending_targets = []
            elif pending_targets:
                logger.info(f"[{date_str}] Phase 2 skipped (past 09:30)")
                pending_targets = []

            # ── Phase 3: 15:30 盘后归因 ──
            if skip_phase3:
                logger.info(f"[{date_str}] Phase 3 skipped (past 15:45), waiting for next day")
            else:
                wait_until(15, 30)
                phase3_daily_sync_and_report(date_str)

            # 等待到第二天
            time.sleep(_HOUR)

        except KeyboardInterrupt:
            logger.info("scheduler stopped by user")
            break
        except Exception as e:
            logger.error(f"scheduler error: {e}")
            time.sleep(MINUTE)


if __name__ == "__main__":
    run_loop()
