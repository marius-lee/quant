"""熔断器 — 标准三态: CLOSED(关闭) → OPEN(开启) → HALF_OPEN(半开)."""

from __future__ import annotations
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

from quant.utils.logger import get_logger

logger = get_logger("data.sources.circuit_breaker")


class CircuitState(Enum):
    CLOSED = "closed"       # 正常, 请求通过
    OPEN = "open"           # 熔断开启, 请求直接拒绝
    HALF_OPEN = "half_open" # 半开, 允许少量探测请求


@dataclass
class CircuitBreakerConfig:
    """熔断器配置."""
    failure_threshold: int = 5            # 连续失败次数触发熔断
    success_threshold: int = 2            # 半开状态下连续成功次数关闭熔断
    timeout_threshold: float = 30.0       # 单次请求超时阈值(秒)
    error_rate_threshold: float = 0.5     # 错误率阈值(滑动窗口)
    window_size: int = 100                # 滑动窗口大小(请求数)
    recovery_timeout: float = 300.0       # OPEN→HALF_OPEN 等待时间(秒)
    half_open_max_calls: int = 3          # 半开状态最大并发探测数


@dataclass
class CircuitBreakerStats:
    """熔断器统计."""
    state: CircuitState = CircuitState.CLOSED
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_failure_time: float = 0
    last_state_change: float = field(default_factory=time.time)
    half_open_calls: int = 0

    @property
    def error_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.failed_calls / self.total_calls


class CircuitBreaker:
    """熔断器核心实现.

    状态转换:
      CLOSED --连续失败/错误率超阈值--> OPEN
      OPEN --recovery_timeout 后--> HALF_OPEN
      HALF_OPEN --连续成功 success_threshold 次--> CLOSED
      HALF_OPEN --任一失败--> OPEN
    """

    def __init__(self, name: str, config: CircuitBreakerConfig | None = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._stats = CircuitBreakerStats()
        self._lock = threading.RLock()
        self._window: deque[tuple[bool, float]] = deque(maxlen=self.config.window_size)  # (success, latency)

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._check_state_transition()
            return self._stats.state

    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    @property
    def stats(self) -> CircuitBreakerStats:
        with self._lock:
            return CircuitBreakerStats(
                state=self._stats.state,
                total_calls=self._stats.total_calls,
                successful_calls=self._stats.successful_calls,
                failed_calls=self._stats.failed_calls,
                consecutive_failures=self._stats.consecutive_failures,
                consecutive_successes=self._stats.consecutive_successes,
                last_failure_time=self._stats.last_failure_time,
                last_state_change=self._stats.last_state_change,
                half_open_calls=self._stats.half_open_calls,
            )

    def allow_request(self) -> bool:
        """检查是否允许请求通过."""
        with self._lock:
            self._check_state_transition()

            if self._stats.state == CircuitState.CLOSED:
                return True

            if self._stats.state == CircuitState.OPEN:
                return False

            # HALF_OPEN: 限制并发探测数
            if self._stats.half_open_calls < self.config.half_open_max_calls:
                self._stats.half_open_calls += 1
                return True
            return False

    def record_result(self, success: bool, latency: float):
        """记录请求结果."""
        with self._lock:
            self._stats.total_calls += 1
            self._window.append((success, latency))

            if success:
                self._stats.successful_calls += 1
                self._stats.consecutive_failures = 0
                self._stats.consecutive_successes += 1
            else:
                self._stats.failed_calls += 1
                self._stats.consecutive_successes = 0
                self._stats.consecutive_failures += 1
                self._stats.last_failure_time = time.time()

            # HALF_OPEN 状态特殊处理
            if self._stats.state == CircuitState.HALF_OPEN:
                self._stats.half_open_calls = max(0, self._stats.half_open_calls - 1)
                if success:
                    if self._stats.consecutive_successes >= self.config.success_threshold:
                        self._transition_to(CircuitState.CLOSED)
                else:
                    self._transition_to(CircuitState.OPEN)

    def _check_state_transition(self):
        """检查状态转换条件."""
        now = time.time()

        if self._stats.state == CircuitState.CLOSED:
            # 检查连续失败
            if self._stats.consecutive_failures >= self.config.failure_threshold:
                self._transition_to(CircuitState.OPEN)
                return

            # 检查滑动窗口错误率
            if len(self._window) >= 10:  # 最小样本数
                recent_failures = sum(1 for s, _ in self._window if not s)
                if recent_failures / len(self._window) >= self.config.error_rate_threshold:
                    self._transition_to(CircuitState.OPEN)
                    return

            # 检查超时率
            if len(self._window) >= 10:
                timeouts = sum(1 for _, l in self._window if l > self.config.timeout_threshold)
                if timeouts / len(self._window) >= self.config.error_rate_threshold:
                    self._transition_to(CircuitState.OPEN)

        elif self._stats.state == CircuitState.OPEN:
            # 检查是否进入 HALF_OPEN
            if now - self._stats.last_state_change >= self.config.recovery_timeout:
                self._transition_to(CircuitState.HALF_OPEN)

    def _transition_to(self, new_state: CircuitState):
        """状态转换."""
        old_state = self._stats.state
        self._stats.state = new_state
        self._stats.last_state_change = time.time()

        if new_state == CircuitState.CLOSED:
            self._stats.consecutive_failures = 0
            self._stats.consecutive_successes = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._stats.half_open_calls = 0
            self._stats.consecutive_successes = 0

        logger.warning(f"[{self.name}] circuit breaker: {old_state.value} → {new_state.value}")

    def reset(self):
        """手动重置熔断器."""
        with self._lock:
            self._stats = CircuitBreakerStats()
            self._window.clear()
            logger.info(f"[{self.name}] circuit breaker manually reset")

    def force_open(self):
        """强制打开熔断器(运维用)."""
        with self._lock:
            self._transition_to(CircuitState.OPEN)

    def force_close(self):
        """强制关闭熔断器(运维用)."""
        with self._lock:
            self._transition_to(CircuitState.CLOSED)