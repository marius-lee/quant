"""Uncaught exception hook — crash + stderr → log.

功能:
1. sys.excepthook: 未捕获异常 traceback 写入 quant.crash → app.log
2. sys.stderr: 所有 stderr 输出同步写入 quant.stderr → app.log（保留终端输出）

Usage: at the top of every entry-point script:
    from utils.excepthook import setup; setup()
"""

import sys as _sys
import traceback as _traceback
import logging as _logging


def setup():
    """Install the global exception hook + stderr capture.  Idempotent."""
    _original = _sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        _logging.getLogger("quant.crash").error(
            "\nUNCAUGHT EXCEPTION:\n"
            + "".join(_traceback.format_exception(exc_type, exc_value, exc_tb)).strip()
        )
        _original(exc_type, exc_value, exc_tb)

    _sys.excepthook = _hook

    # ── stderr → log: tee 模式，保留终端输出同步写 app.log ──
    class _StderrLogger:
        _recursing = False

        def __init__(self, real):
            self._real = real
            self._buf = ""
        def write(self, s):
            self._real.write(s)
            self._real.flush()
            self._buf += s
            if "\n" in self._buf:
                if _StderrLogger._recursing:
                    self._buf = self._buf.split("\n")[-1]
                    return
                _StderrLogger._recursing = True
                try:
                    for line in self._buf.split("\n")[:-1]:
                        stripped = line.strip()
                        if stripped:
                            _logging.getLogger("quant.stderr").info(line)
                    self._buf = self._buf.split("\n")[-1]
                finally:
                    _StderrLogger._recursing = False
        def flush(self):
            self._real.flush()

    if not isinstance(_sys.stderr, _StderrLogger):
        _sys.stderr = _StderrLogger(_sys.stderr)
