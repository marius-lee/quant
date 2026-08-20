"""分布式限流器 — 支持单进程令牌桶 + 跨进程文件锁.

设计:
  - TokenBucketLimiter: 单进程内存令牌桶(高性能)
  - DistributedRateLimiter: 基于 fcntl 文件锁的跨进程令牌桶
  - 自动降级: 无 fcntl(Windows)时退化为单进程模式
"""

from __future__ import annotations
import json
import os
import time
import threading
from pathlib import Path
from dataclasses import dataclass
from contextlib import contextmanager

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    fcntl = None
    HAS_FCNTL = False

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

logger = get_logger("data.sources.rate_limiter")


@dataclass
class RateLimitConfig:
    """限流配置."""
    rps: float = 10.0          # 每秒请求数
    burst: int = 20            # 突发桶容量
    daily_limit: int | None = None  # 日限额
    key_prefix: str = "default"     # 状态文件前缀


class TokenBucketLimiter:
    """单进程令牌桶限流器(线程安全)."""

    def __init__(self, config: RateLimitConfig):
        self.config = config
        self._tokens = float(config.burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._daily_count = 0
        self._daily_reset = time.time() + 86400

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.config.burst, self._tokens + elapsed * self.config.rps)
        self._last_refill = now

        # 日计数重置
        if time.time() >= self._daily_reset:
            self._daily_count = 0
            self._daily_reset = time.time() + 86400

    def acquire(self, tokens: int = 1, timeout: float | None = None) -> bool:
        """获取令牌, 阻塞直到可用或超时."""
        deadline = time.monotonic() + timeout if timeout else None

        while True:
            with self._lock:
                self._refill()

                # 检查日限额
                if self.config.daily_limit and self._daily_count >= self.config.daily_limit:
                    if deadline and time.monotonic() > deadline:
                        return False
                    # 等到第二天
                    wait = self._daily_reset - time.time()
                    if wait > 0:
                        time.sleep(min(wait, 1))
                    continue

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    self._daily_count += 1
                    return True

            if deadline and time.monotonic() > deadline:
                return False

            # 计算下次填充时间
            with self._lock:
                wait = (tokens - self._tokens) / self.config.rps
            time.sleep(max(wait, 0.001))

        return False

    def try_acquire(self, tokens: int = 1) -> bool:
        """非阻塞尝试获取令牌."""
        with self._lock:
            self._refill()
            if self.config.daily_limit and self._daily_count >= self.config.daily_limit:
                return False
            if self._tokens >= tokens:
                self._tokens -= tokens
                self._daily_count += 1
                return True
        return False

    @property
    def available_tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens


class DistributedRateLimiter:
    """跨进程分布式限流器 — 基于文件锁的令牌桶.

    状态文件存储:
      - tokens: 当前令牌数
      - last_refill: 上次填充时间
      - daily_count: 当日计数
      - daily_reset: 重置时间戳

    所有进程共享同一状态文件, 通过 fcntl 锁保证原子性.
    """

    def __init__(self, config: RateLimitConfig, state_dir: str | None = None):
        self.config = config
        self.state_dir = Path(state_dir or _require_cfg("data.sources.state_dir", "/tmp/quant_sources"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / f"{config.key_prefix}_ratelimit.json"
        self.lock_file = self.state_dir / f"{config.key_prefix}_ratelimit.lock"
        self._local = TokenBucketLimiter(config)  # 本地缓存, 减少锁竞争
        self._lock_fd: int | None = None

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return {
                "tokens": float(self.config.burst),
                "last_refill": time.time(),
                "daily_count": 0,
                "daily_reset": time.time() + 86400,
            }

    def _save_state(self, state: dict):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(self.state_file)

    @contextmanager
    def _locked(self):
        """文件锁上下文."""
        if not HAS_FCNTL:
            # Windows 无 fcntl, 退化为本地限流
            yield
            return

        fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def acquire(self, tokens: int = 1, timeout: float | None = None) -> bool:
        """获取令牌(跨进程同步)."""
        # 先尝试本地缓存(快速路径)
        if self._local.try_acquire(tokens):
            return True

        # 本地不足, 同步全局状态
        deadline = time.monotonic() + timeout if timeout else None

        while True:
            with self._locked():
                state = self._load_state()
                now = time.time()

                # 日重置
                if now >= state["daily_reset"]:
                    state["daily_count"] = 0
                    state["daily_reset"] = now + 86400

                # 检查日限额
                if self.config.daily_limit and state["daily_count"] >= self.config.daily_limit:
                    if deadline and time.monotonic() > deadline:
                        return False
                    wait = state["daily_reset"] - now
                    if wait > 0:
                        time.sleep(min(wait, 1))
                    continue

                # 令牌填充
                elapsed = now - state["last_refill"]
                state["tokens"] = min(self.config.burst, state["tokens"] + elapsed * self.config.rps)
                state["last_refill"] = now

                if state["tokens"] >= tokens:
                    state["tokens"] -= tokens
                    state["daily_count"] += 1
                    self._save_state(state)
                    # 同步本地缓存
                    with self._local._lock:
                        self._local._tokens = state["tokens"]
                        self._local._last_refill = state["last_refill"]
                        self._local._daily_count = state["daily_count"]
                        self._local._daily_reset = state["daily_reset"]
                    return True

            if deadline and time.monotonic() > deadline:
                return False

            wait = (tokens - state["tokens"]) / self.config.rps
            time.sleep(max(wait, 0.001))

        return False

    def try_acquire(self, tokens: int = 1) -> bool:
        """非阻塞尝试."""
        if self._local.try_acquire(tokens):
            return True

        with self._locked():
            state = self._load_state()
            now = time.time()

            if now >= state["daily_reset"]:
                state["daily_count"] = 0
                state["daily_reset"] = now + 86400

            if self.config.daily_limit and state["daily_count"] >= self.config.daily_limit:
                return False

            elapsed = now - state["last_refill"]
            state["tokens"] = min(self.config.burst, state["tokens"] + elapsed * self.config.rps)
            state["last_refill"] = now

            if state["tokens"] >= tokens:
                state["tokens"] -= tokens
                state["daily_count"] += 1
                self._save_state(state)
                with self._local._lock:
                    self._local._tokens = state["tokens"]
                    self._local._last_refill = state["last_refill"]
                    self._local._daily_count = state["daily_count"]
                    self._local._daily_reset = state["daily_reset"]
                return True

        return False

    def get_stats(self) -> dict:
        """获取当前状态统计."""
        with self._locked():
            state = self._load_state()
        return {
            "available_tokens": state["tokens"],
            "daily_used": state["daily_count"],
            "daily_limit": self.config.daily_limit,
            "rps": self.config.rps,
            "burst": self.config.burst,
        }