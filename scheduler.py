"""量化系统调度器 — 每个交易日 15:30 自动执行。

流程:
  1. 等交易日 15:30
  2. 跑 daily_sync (更新所有数据源)
  3. 跑 pipeline (选股+调仓)
  
启动:
  PYTHONPATH=. .venv/bin/python3 scheduler.py &
  launchd 开机自启 (见 com.quant.scheduler.plist)
"""

import time, sys
from datetime import datetime, timedelta
from utils.logger import get_logger
from execution.calendar import is_trading_day, is_market_open

logger = get_logger("scheduler")
MINUTE = 60


def wait_until(target_hour: int = 15, target_minute: int = 30) -> bool:
    """休眠至下一个目标时间。返回是否到达目标。"""
    now = datetime.now()
    target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if now > target:
        target += timedelta(days=1)

    wait_sec = (target - now).total_seconds()
    if wait_sec > 0:
        logger.info(f"scheduler: sleeping {wait_sec:.0f}s until {target.strftime('%H:%M')}")
        time.sleep(min(wait_sec, MINUTE))
        return False
    return True


def run_loop():
    """主循环: 交易日 15:30 自动执行 daily_sync → pipeline。"""
    logger.info("scheduler started — waiting for next trading day 15:30")

    while True:
        try:
            dt = datetime.now()

            if not is_trading_day(dt.date()):
                time.sleep(MINUTE)
                continue

            if not wait_until(15, 30):
                continue

            date_str = dt.strftime("%Y-%m-%d")
            logger.info(f"[{date_str}] 15:30 — starting daily sync + pipeline")

            # Step A: 数据同步
            from daily_sync import run as sync_run
            try:
                sync_results = sync_run(date_str)
                logger.info(f"[{date_str}] daily_sync: {sync_results}")
            except Exception as e:
                logger.error(f"[{date_str}] daily_sync failed: {e}")

            # Step B: Pipeline 选股 + 调仓
            from pipeline import run
            try:
                result = run(date_str)
                steps_ok = [k for k, v in result.get('steps', {}).items() if v.get('status') == 'ok']
                logger.info(f"[{date_str}] pipeline: {result.get('elapsed_sec', 0)}s, {steps_ok}")
            except Exception as e:
                logger.error(f"[{date_str}] pipeline failed: {e}")

            # 下一个交易日
            time.sleep(60 * 60)

        except KeyboardInterrupt:
            logger.info("scheduler stopped by user")
            break
        except Exception as e:
            logger.error(f"scheduler error: {e}")
            time.sleep(MINUTE)


if __name__ == "__main__":
    run_loop()
