"""调度器 — 交易日 09:25 盘前 / 15:30 盘后 自动执行 pipeline。

仅在 web app 启动时作为 daemon 线程启动，不独立运行。
调度逻辑: 每 60s 检查一次，达到目标时间且今日未运行过则触发。
"""
import time as _time
import threading
from datetime import datetime, time

from utils.logger import get_logger

logger = get_logger("execution.scheduler")

# 调度时间 (24小时制)
MORNING_RUN = time(9, 25)   # 盘前生成信号
EOD_RUN = time(15, 30)      # 盘后更新日线 + 生成信号


def _seconds_until(target: time) -> float:
    """计算距离下一个 target 时间的秒数。"""
    now = datetime.now()
    target_dt = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if target_dt <= now:
        return (target_dt - now).total_seconds() + 86400
    return (target_dt - now).total_seconds()


def _summarize_today(log_key: str) -> str:
    """生成今日摘要，用于 broker update。"""
    today = datetime.now().strftime("%Y-%m-%d")
    return f"{log_key} {today}"


def start(broker):
    """启动调度线程 (daemon).

    Args:
        broker: StateBroker 实例, 用于更新状态
    """
    from execution.calendar import is_trading_day

    def _loop():
        ran_morning = None   # 今日是否已跑盘前
        ran_eod = None       # 今日是否已跑盘后
        today = None

        logger.info("scheduler started: 09:25 morning run, 15:30 EOD run")

        while True:
            now = datetime.now()
            current_day = now.strftime("%Y-%m-%d")

            # 跨天重置
            if current_day != today:
                today = current_day
                ran_morning = False
                ran_eod = False

            # 非交易日跳过
            if not is_trading_day():
                _time.sleep(60)
                continue

            hhmm = time(now.hour, now.minute)

            # 盘前 09:25
            if hhmm >= MORNING_RUN and not ran_morning:
                logger.info(f"scheduler: morning run {today}")
                broker.update({"status": "running", "progress": f"morning_pipeline {today}"})
                try:
                    import pipeline
                    pipeline.run(date_str=today)
                except Exception as e:
                    logger.error(f"scheduler morning run failed: {e}")
                    broker.update({"status": "error", "progress": str(e)[:100]})
                else:
                    broker.update({"status": "idle", "progress": f"morning_done {today}"})
                ran_morning = True

            # 盘后 15:30
            if hhmm >= EOD_RUN and not ran_eod:
                logger.info(f"scheduler: EOD run {today}")
                broker.update({"status": "running", "progress": f"eod_pipeline {today}"})
                try:
                    import pipeline
                    pipeline.run(date_str=today)
                except Exception as e:
                    logger.error(f"scheduler EOD run failed: {e}")
                    broker.update({"status": "error", "progress": str(e)[:100]})
                else:
                    broker.update({"status": "idle", "progress": f"eod_done {today}"})
                ran_eod = True

            _time.sleep(60)

    t = threading.Thread(target=_loop, daemon=True, name="quant-scheduler")
    t.start()
    logger.info("scheduler thread launched")
