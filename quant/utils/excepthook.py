"""Uncaught exception hook — crash → log.

只捕获未处理异常，不拦截 stderr。
stderr 是给开发者和运维看的，不应混入应用日志。
通过 traceback 帧检测 crash 是否发生在 backtest/evaluation 上下文中，
决定路由到 app.log 还是 backtest.log。

Usage: at the top of every entry-point script:
    from quant.utils.excepthook import setup; setup()
"""

import sys as _sys
import traceback as _traceback
import logging as _logging


def _is_backtest_context(exc_tb) -> bool:
    """Traceback 中是否有 backtest/evaluation 模块帧."""
    while exc_tb is not None:
        fname = exc_tb.tb_frame.f_code.co_filename
        if '/backtest/' in fname or '/evaluation/' in fname:
            return True
        if 'smoke_test.py' in fname:
            return True
        exc_tb = exc_tb.tb_next
    return False


def setup():
    """Install the global exception hook. Idempotent."""
    _original = _sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        _is_bt = _is_backtest_context(exc_tb)
        msg = ("\nUNCAUGHT EXCEPTION:\n"
               + "".join(_traceback.format_exception(exc_type, exc_value, exc_tb)).strip())
        # 路由: backtest 上下文 → backtest.log; 否则 → app.log
        if _is_bt:
            _logging.getLogger("quant.backtest.crash").error(msg)
            # 不回退到原始 hook — 避免回测崩溃同时写进 app.log (重复日志)
            return
        else:
            _logging.getLogger("quant.crash").error(msg)
            _original(exc_type, exc_value, exc_tb)

    _sys.excepthook = _hook
