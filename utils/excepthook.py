"""Uncaught exception hook for crash-to-log capture.

After removing try/except shells, errors propagate naturally but bypass
the log file.  This hook writes every uncaught exception to app.log while
preserving the normal stderr traceback.

Usage: at the top of every entry-point script:
    from utils.excepthook import setup; setup()
"""

import sys as _sys
import traceback as _traceback
import logging as _logging


def setup():
    """Install the global exception hook.  Idempotent — safe to call multiple times."""
    _original = _sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        _logging.getLogger("quant.crash").error(
            "\nUNCAUGHT EXCEPTION:\n"
            + "".join(_traceback.format_exception(exc_type, exc_value, exc_tb)).strip()
        )
        _original(exc_type, exc_value, exc_tb)

    _sys.excepthook = _hook
