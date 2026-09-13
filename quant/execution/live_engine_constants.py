"""实盘订单执行引擎 - 智能路由、分片执行、TWAP/VWAP/冰山单、成本模型校准."""

from __future__ import annotations
import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from quant.execution.broker_adapter import (
    BrokerAdapterBase, BrokerConfig, BrokerManager, BrokerType,
    OrderRequest, OrderResponse, OrderSide, OrderType, OrderStatus, TimeInForce,
    Trade, Position, Account, RateLimiter
)
from quant.execution.cost import CostModel
from quant.execution.execution_model import ExecutionContext, ExecutionResult, LiveExecutionModel
from quant.execution.engine import ExecutionEngine, Order
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

logger = get_logger("execution.live_engine")


