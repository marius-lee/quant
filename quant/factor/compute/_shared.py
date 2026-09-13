from quant.config.paths import MARKET_DB
"""Shared utilities for factor compute sub-modules."""
import os as _os

from quant.utils.logger import get_logger

logger = get_logger("factor.compute._shared")



def _market_db_path():
    return MARKET_DB
