"""统一日志系统 — 所有模块通过 get_logger(__name__) 获取 logger。

日志输出:
  - 控制台: INFO 级别，关键路径和进度
  - app.log: 非回测模块日轮转 (INFO+)
  - backtest.log: 回测模块日轮转 (INFO+)
  - quant.log: JSON 结构化全量 (DEBUG)
  - 轮转: 单文件最大 50MB，保留最近 5 个
"""

import logging
import os
import sys
import threading
import contextvars
import json
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

_log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
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

    # ── 人类可读文件日志: 按模块名分离 ──
    _fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-5s %(name)s | %(trace_id)s%(message)s",
        datefmt="%m-%d %H:%M:%S", defaults={"trace_id": ""}
    )
    _is_backtest = lambda r: (
    r.name.startswith("quant.backtest.") or
    r.name.startswith("backtest.") or
    r.name.startswith("quant.evaluation.") or
    r.name.startswith("evaluation.")
)

    # app.log: 非回测 (web, pipeline, scheduler, factor...)
    _app_handler = TimedRotatingFileHandler(
        os.path.join(_log_dir, "app.log"), when="midnight", interval=1,
        backupCount=10, encoding="utf-8"
    )
    _app_handler.setLevel(logging.INFO)
    _app_handler.setFormatter(_fmt)
    _app_handler.addFilter(lambda r: not _is_backtest(r))
    root.addHandler(_app_handler)

    # backtest.log: 回测专用
    _bt_handler = TimedRotatingFileHandler(
        os.path.join(_log_dir, "backtest.log"), when="midnight", interval=1,
        backupCount=10, encoding="utf-8"
    )
    _bt_handler.setLevel(logging.INFO)
    _bt_handler.setFormatter(_fmt)
    _bt_handler.addFilter(_is_backtest)
    root.addHandler(_bt_handler)

    # 文件: JSON 结构化 DEBUG (模板9 T1)
    file_handler = RotatingFileHandler(
        _log_file, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_JsonFormatter())
    root.addHandler(file_handler)

    # ── 未捕获异常自动写入日志 ──
    def _log_uncaught(t, v, tb):
        import traceback
        root.critical("未捕获异常: %s: %s\n%s", t.__name__, v, "".join(traceback.format_tb(tb)))
    if sys.excepthook is sys.__excepthook__:  # 不覆盖 excepthook.setup() 的 hook
        sys.excepthook = _log_uncaught


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
