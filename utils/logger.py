"""统一日志系统 — 所有模块通过 get_logger(__name__) 获取 logger。

日志输出:
  - 控制台: INFO 级别，关键路径和进度
  - 文件: DEBUG 级别，完整记录到 logs/quant.log
  - 轮转: 单文件最大 10MB，保留最近 5 个
"""

import logging
import os
import threading
from logging.handlers import RotatingFileHandler

_log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
_log_file = os.path.join(_log_dir, "quant.log")
_initialized = False
_lock = threading.Lock()


def _init():
    global _initialized
    with _lock:
        if _initialized:
            return
        _initialized = True

    os.makedirs(_log_dir, exist_ok=True)

    root = logging.getLogger("quant")
    root.setLevel(logging.DEBUG)

    # 控制台: INFO
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
        datefmt="%m-%d %H:%M:%S"
    ))
    root.addHandler(console)

    # 文件: DEBUG (轮转)
    file_handler = RotatingFileHandler(
        _log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """获取模块级 logger。用法: logger = get_logger(__name__)"""
    _init()
    return logging.getLogger(f"quant.{name}")
