"""量化系统调度器 — 每个交易日的 15:30 触发 Pipeline。

策略: 非交易时间休眠 1 分钟, 交易日自动运行 pipeline。
"""

import time
import sys
from datetime import datetime
from utils.logger import get_logger
from execution.calendar import is_trading_day, is_market_open

logger = get_logger("scheduler")

MINUTE = 60


def wait_until(target_hour: int = 15, target_minute: int = 30):
    """休眠至下一个目标时间。"""
    now = datetime.now()
    target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if now > target:
        # 已过今天的目标时间, 等明天
        from datetime import timedelta
        target += timedelta(days=1)

    wait_sec = (target - now).total_seconds()
    if wait_sec > 0:
        logger.info(f"scheduler: sleeping {wait_sec:.0f}s until {target.strftime('%H:%M')}")
        time.sleep(min(wait_sec, MINUTE))  # 每次最多睡1分钟, 便于中断
        return False  # 还没到
    return True  # 到了


def run_loop():
    """主循环: 交易日 15:30 自动执行 pipeline。"""
    logger.info("scheduler started — waiting for next trading day 15:30")

    from pipeline import run

    while True:
        try:
            dt = datetime.now()

            if not is_trading_day(dt.date()):
                time.sleep(MINUTE)
                continue

            if not wait_until(15, 30):
                continue

            # 到达 15:30, 且是交易日, 执行 pipeline
            logger.info(f"scheduler: triggering pipeline for {dt.strftime('%Y-%m-%d')}")
            result = run(dt.strftime("%Y-%m-%d"))
            logger.info(f"scheduler: pipeline result — {result.get('elapsed_sec', 0)}s, "
                       f"steps: {[k for k, v in result.get('steps', {}).items() if v.get('status') == 'ok']}")

            # 执行完后休眠到下一个交易日
            time.sleep(60 * 60)  # 1 小时

        except KeyboardInterrupt:
            logger.info("scheduler stopped by user")
            break
        except Exception as e:
            logger.error(f"scheduler error: {e}")
            time.sleep(MINUTE)


if __name__ == "__main__":
    run_loop()
