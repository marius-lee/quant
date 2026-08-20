"""熔断器基类与四种具体实现 — 多级别熔断架构"""

from __future__ import annotations
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Callable, Any

from quant.utils.logger import get_logger

logger = get_logger("risk.circuit_breaker")


class CircuitState(Enum):
    """熔断器状态"""
    CLOSED = "closed"       # 正常运行
    HALF_OPEN = "half_open" # 半开：尝试恢复
    OPEN = "open"           # 熔断开启


class CircuitLevel(Enum):
    """熔断器级别"""
    ACCOUNT = "account"     # 账户级
    STRATEGY = "strategy"   # 策略级
    SYMBOL = "symbol"       # 品种级
    MARKET = "market"       # 全市场级


@dataclass
class CircuitBreakerConfig:
    """熔断器配置"""
    name: str
    level: str  # "account" | "strategy" | "symbol" | "market"
    # 触发条件
    failure_threshold: int = 5          # 连续失败次数
    error_rate_threshold: float = 0.5   # 错误率阈值
    timeout_threshold: float = 30.0     # 超时阈值(秒)
    # 恢复条件
    recovery_timeout: float = 60.0      # 熔断后等待恢复时间(秒)
    success_threshold: int = 3          # 半开状态下连续成功次数
    # 作用域
    scope: str = "default"              # 作用域标识
    tags: Dict[str, str] = None


class CircuitState:
    """熔断器状态常量"""
    CLOSED = "closed"       # 正常运行
    HALF_OPEN = "half_open" # 半开：尝试恢复
    OPEN = "open"           # 熔断开启


class CircuitBreakerBase:
    """熔断器基类"""
    
    def __init__(self, config: dict):
        self.config = config
        self.name = config.get("name", "unknown")
        self.level = config.get("level", "unknown")
        self.scope = config.get("scope", "default")
        
        # 触发条件
        self.failure_threshold = config.get("failure_threshold", 5)
        self.error_rate_threshold = config.get("error_rate_threshold", 0.5)
        self.timeout_threshold = config.get("timeout_threshold", 30.0)
        
        # 恢复条件
        self.recovery_timeout = config.get("recovery_timeout", 60.0)
        self.success_threshold = config.get("success_threshold", 3)
        
        # 状态
        self.state = "closed"  # closed | half_open | open
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None
        self._state_change_time = time.time()
        self._lock = threading.RLock()
        self._callbacks = []
    
    def record_success(self):
        """记录成功"""
        with self._lock:
            self._failure_count = 0
            self._success_count += 1
            if self.state == "half_open":
                if self._success_count >= self._config.get("success_threshold", 3):
                    self._transition_to("closed")
    
    def record_failure(self, error: Exception = None):
        """记录失败"""
        with self._lock:
            self._failure_count += 1
            self._success_count = 0
            self._last_failure_time = time.time()
            if self._check_trigger():
                self._transition_to("open")
    
    def _check_trigger(self) -> bool:
        """检查是否触发熔断"""
        # 连续失败次数
        if self._failure_count >= self._config.get("failure_threshold", 5):
            return True
        return False
    
    def _transition_to(self, new_state: str):
        """状态转换"""
        old_state = self.state
        self.state = new_state
        self._state_change_time = time.time()
        if new_state == "open":
            self._on_open()
        elif new_state == "half_open":
            self._on_half_open()
        elif new_state == "closed":
            self._on_closed()
        self._notify_state_change(old_state, new_state)
    
    def _on_open(self):
        pass
    
    def _on_half_open(self):
        pass
    
    def _on_closed(self):
        pass
    
    def _notify_state_change(self, old_state: str, new_state: str):
        for cb in self._callbacks:
            try:
                cb(self, old_state, new_state)
            except Exception:
                pass
    
    def allow_request(self) -> bool:
        """检查是否允许请求通过"""
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "open":
                if time.time() - self._state_change_time >= self._config.get("recovery_timeout", 60):
                    self._transition_to("half_open")
                    return True
                return False
            # half_open
            return True
    
    def on_state_change(self, callback):
        self._callbacks.append(callback)
    
    def _transition_to(self, new_state: str):
        """内部状态转换"""
        old_state = self.state
        self.state = new_state
        self._state_change_time = time.time()
        if new_state == "open":
            self._on_open()
        elif new_state == "half_open":
            self._on_half_open()
        elif new_state == "closed":
            self._on_closed()
        self._notify_state_change(old_state, new_state)
    
    def _on_open(self):
        pass
    
    def _on_half_open(self):
        pass
    
    def _on_closed(self):
        pass


class AccountCircuitBreaker:
    """账户级熔断器：监控账户级风险指标"""
    
    def __init__(self, config: dict):
        self._config = config
        self._state = "closed"
        self._failure_count = 0
        self._success_count = 0
        self._lock = threading.RLock()
    
    def check_trigger(self, metrics: dict) -> bool:
        """检查是否触发熔断"""
        # 保证金率 < 130%
        if metrics.get("margin_ratio", 1.0) < 1.3:
            return True
        # 单日亏损 > 5%
        if metrics.get("daily_pnl_pct", 0) < -0.05:
            return True
        # 保证金使用率 > 90%
        if metrics.get("margin_usage", 0) > 0.9:
            return True
        return False
    
    def on_open(self):
        """触发熔断时的处理"""
        logger.warning("账户级熔断触发：停止开仓，仅允许平仓")
        # 这里可以集成 ExecutionEngine 禁用开仓


class StrategyCircuitBreaker:
    """策略级熔断器"""
    
    def check_trigger(self, metrics: dict) -> bool:
        # 连续亏损
        if metrics.get("consecutive_losses", 0) >= 5:
            return True
        # 回撤超过 15%
        if metrics.get("max_drawdown", 0) > 0.15:
            return True
        # 胜率过低
        if metrics.get("total_trades", 0) > 20 and metrics.get("win_rate", 1) < 0.35:
            return True
        return False
    
    def on_open(self):
        logger.warning("策略级熔断触发：停止该策略开仓")


class SymbolCircuitBreaker:
    """品种级熔断器"""
    
    def check_trigger(self, metrics: dict) -> bool:
        # 异常波动
        if metrics.get("intraday_volatility", 0) > 0.08:
            return True
        # 流动性枯竭
        if metrics.get("avg_spread_bps", 0) > 50:
            return True
        # 异常成交量
        if metrics.get("volume_ratio", 0) > 10:
            return True
        return False


class MarketCircuitBreaker:
    """全市场熔断器"""
    
    def check_trigger(self, metrics: dict) -> bool:
        # 全市场跌幅
        if metrics.get("market_change_pct", 0) < -0.05:
            return True
        # 市场宽度极度恶化
        if metrics.get("advance_decline_ratio", 1) < 0.2:
            return True
        # VIX 飙升
        if metrics.get("vix", 0) > 50:
            return True
        return False


class CircuitBreakerManager:
    """熔断器管理器"""
    
    def __init__(self):
        self._breakers = {}
        self._lock = threading.RLock()
    
    def register(self, name: str, breaker):
        self._breakers[name] = breaker
    
    def check_all(self) -> dict:
        """检查所有熔断器"""
        triggered = []
        for name, breaker in self._breakers.items():
            if hasattr(breaker, 'check_trigger'):
                # 这里需要传入相应的 metrics
                pass
        return triggered
    
    def force_open(self, name: str):
        pass
    
    def force_close(self, name: str):
        pass


def get_circuit_breaker_manager():
    if not hasattr(_circuit_breaker_manager, '_instance'):
        _circuit_breaker_manager._instance = _CircuitBreakerManager()
    return _circuit_breaker_manager._instance


class _CircuitBreakerManager:
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        self._breakers = {}
        self._lock = threading.RLock()
    
    def register(self, name: str, breaker):
        self._breakers[name] = breaker
    
    def check_all(self) -> list:
        return []