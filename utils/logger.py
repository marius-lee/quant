"""统一日志系统 — 所有模块通过 get_logger(__name__) 获取 logger。

日志输出:
  - 控制台: INFO 级别，关键路径和进度
  - 文件: DEBUG 级别，完整记录到 logs/quant.log
  - 轮转: 单文件最大 10MB，保留最近 5 个
"""

import logging
import os
import threading
import contextvars
import json
from logging.handlers import RotatingFileHandler

_log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
_log_file = os.path.join(_log_dir, "quant.log")
_initialized = False
_lock = threading.Lock()

# ── trace_id context (模板9: 链路追踪) ──
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar('trace_id', default='')

def set_trace_id(tid: str):
    _trace_id_var.set(tid)

def get_trace_id() -> str:
    return _trace_id_var.get()

def _init():
    global _initialized
    with _lock:
        if _initialized:
            return
        _initialized = True

    os.makedirs(_log_dir, exist_ok=True)

    root = logging.getLogger("quant")
    root.setLevel(logging.DEBUG)

    # 控制台: INFO, 人类可读 + trace_id 前缀
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s %(name)s | %(trace_id)s%(message)s",
        datefmt="%m-%d %H:%M:%S",
        defaults={"trace_id": ""}
    ))
    root.addHandler(console)

    # 文件: JSON 结构化 DEBUG (模板9 T1)
    file_handler = RotatingFileHandler(
        _log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_JsonFormatter())
    root.addHandler(file_handler)


class _JsonFormatter(logging.Formatter):
    """模板9 T1: JSON 结构化日志, 便于 grep + jq 分析."""
    def format(self, record):
        obj = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "module": record.name,
            "msg": record.getMessage(),
            "lineno": record.lineno,
        }
        tid = get_trace_id()
        if tid:
            obj["trace_id"] = tid
        if record.exc_info and record.exc_info[1]:
            obj["exc"] = str(record.exc_info[1])
        return json.dumps(obj, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    """获取模块级 logger。用法: logger = get_logger(__name__)"""
    _init()
    # 注入 trace_id 到 log record (Python logging 不自动读 contextvars)
    logger = logging.getLogger(f"quant.{name}")
    return _TraceLoggerAdapter(logger)


class _TraceLoggerAdapter(logging.LoggerAdapter):
    """自动注入 trace_id 到每条日志."""
    def process(self, msg, kwargs):
        tid = get_trace_id()
        extra = kwargs.get('extra', {})
        extra['trace_id'] = f"[{tid}] " if tid else ""
        kwargs['extra'] = extra
        return msg, kwargs
