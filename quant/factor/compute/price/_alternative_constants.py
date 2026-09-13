"""价量因子子模块。"""

import traceback

import numpy as np
import pandas as pd
import sqlite3
import os as _os
from typing import Optional

from quant.utils.date import to_str
from quant.config.constants import *
from quant.factor.registry import _cs_zscore, _db_connect, _FIN_FACTORS, _shared_limit_conn
from quant.factor.compute._shared import _market_db_path

from quant.utils.logger import get_logger as _get_logger
from quant.data.repos._base import DatabaseManager

_log = _get_logger("factor.compute")

# ── ztd 预计算缓存: 消除每交易日重复 SQLite 查询 ──
# key: date_str → value: Series(index=symbol, value=ztd_ratio)
_ztd_cache: dict = {}


