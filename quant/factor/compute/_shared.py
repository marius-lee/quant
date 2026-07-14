"""Shared utilities for factor compute sub-modules."""
import os as _os


def _market_db_path():
    return _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "data", "market.db")
