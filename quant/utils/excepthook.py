"""Uncaught exception hook — crash → log.

只捕获未处理异常，不拦截 stderr。
stderr 是给开发者和运维看的，不应混入应用日志。

Usage: at the top of every entry-point script:
    from quant.utils.excepthook import setup; setup()
"""

import sys as _sys
import traceback as _traceback
import logging as _logging


def setup():
    """Install the global exception hook. Idempotent."""
    _original = _sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        _logging.getLogger("quant.crash").error(
            "\nUNCAUGHT EXCEPTION:\n"
            + "".join(_traceback.format_exception(exc_type, exc_value, exc_tb)).strip()
        )
        _original(exc_type, exc_value, exc_tb)

    _sys.excepthook = _hook
