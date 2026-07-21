#!/usr/bin/env python3
"""API 数据拉取缓存层: 限流、重试。P88: Redis 已移除，纯本地实现。

消费者 (pipeline/web) 从 SQLite 读数据，本模块仅做 API 限流。

模块组件:
- NoopBackend            — 本地空缓存 (无跨进程共享需求)
- RateLimiter            — 线程内令牌桶限流
- RetryConfig            — 指数退避重试
- DataCache              — 本地线程缓存 (无持久化)
- with_fallback          — 多数据源链式降级
"""
import abc
import functools
import logging
import time
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger("quant.data.cache")


class CacheBackend(abc.ABC):
    """缓存后端抽象 (P88: 仅保留接口兼容性)."""
    @abc.abstractmethod
    def get(self, key: str) -> Any: ...
    @abc.abstractmethod
    def set(self, key: str, value: Any, ttl: int): ...
    @abc.abstractmethod
    def delete(self, key: str): ...
    @abc.abstractmethod
    def check_rate_limit(self, namespace: str, max_calls: int, window_sec: int) -> bool: ...
    @abc.abstractmethod
    def acquire_lock(self, lock_name: str, ttl: int) -> bool: ...
    @abc.abstractmethod
    def release_lock(self, lock_name: str): ...
    @abc.abstractmethod
    def ping(self) -> bool: ...


class NoopBackend(CacheBackend):
    """纯本地实现 — 限流用线程本地令牌桶，缓存用线程本地 dict。"""

    def __init__(self):
        self._cache: dict = {}
        self._buckets: dict = {}  # namespace → (tokens, last_refill_ts)

    def get(self, key: str) -> Any:
        val = self._cache.get(key)
        if val:
            ts, data = val
            if time.time() - ts < 3600:
                return data
            del self._cache[key]
        return None

    def set(self, key: str, value: Any, ttl: int):
        self._cache[key] = (time.time() + ttl, value)

    def delete(self, key: str):
        self._cache.pop(key, None)

    def check_rate_limit(self, namespace: str, max_calls: int, window_sec: int, burst: int = None) -> bool:
        """线程本地令牌桶 — 滑动窗口计数。

        max_calls: 每分钟允许调用数, 控制 refill 速率 (tokens/s = max_calls/window_sec)
        burst:     桶容量上限 (默认 = max_calls), 限制突发并发数
        来源: 2026-07-21 burst=2 导制 refill 速率降至 2/60 tokens/s 的根因分析
        """
        cap = burst if burst is not None else max_calls
        now = time.time()
        bucket = self._buckets.get(namespace)
        if not bucket:
            self._buckets[namespace] = (cap - 1, now)
            return True
        tokens, last = bucket
        elapsed = now - last
        tokens = min(cap, tokens + int(elapsed / window_sec * max_calls))
        if tokens > 0:
            self._buckets[namespace] = (tokens - 1, now)
            return True
        # 被拒绝时依然更新 last — 防止 last 冻结导致 elapsed 不增加，tokens 永不 refill。
        # 来源: 2026-07-21 tushare 超限全链路根因分析
        self._buckets[namespace] = (tokens, now)
        return False

    def acquire_lock(self, lock_name: str, ttl: int) -> bool:
        if lock_name in self._cache:
            ts, _ = self._cache[lock_name]
            if time.time() - ts < ttl:
                return False
        self._cache[lock_name] = (time.time() + ttl, True)
        return True

    def release_lock(self, lock_name: str):
        self._cache.pop(lock_name, None)

    def ping(self) -> bool:
        return True


# 全局单例
_backend = NoopBackend()


def get_backend(config: dict = None) -> CacheBackend:
    """返回缓存后端 (P88: 始终返回本地 NoopBackend)。"""
    return _backend


def reset_backend():
    """重置后端 (测试用)。"""
    global _backend
    _backend = NoopBackend()


class RateLimiter:
    """本地令牌桶限流器。

    namespace:        限流标识 (如 "tushare_stock_list")
    calls_per_minute: 每分钟允许调用数
    burst:            突发容量 (默认 = calls_per_minute)
    """

    def __init__(self, namespace: str, calls_per_minute: int = 200,
                 burst: int = None, backend: CacheBackend = None):
        self.namespace = namespace
        self.calls_per_minute = calls_per_minute
        self.burst = burst or calls_per_minute
        self._backend = backend or _backend

    def is_allowed(self) -> bool:
        # max_calls=calls_per_minute 控制 refill 速率, burst 限制桶容量上限
        # 来源: 2026-07-21 burst=2 导制 refill 降至 2/60 tokens/s 的根因分析
        return self._backend.check_rate_limit(
            self.namespace, self.calls_per_minute, 60, self.burst)

    def wait_if_needed(self, timeout: float = 60.0):
        """阻塞等待直到允许调用 (最多 timeout 秒)。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_allowed():
                return True
            time.sleep(1.0)
        return False

    def wait(self):
        """阻塞等待直到允许调用（wait_if_needed 的简写别名）。"""
        self.wait_if_needed()

    def __enter__(self):
        if not self.is_allowed():
            self.wait_if_needed()
        return self

    def __exit__(self, *args):
        pass


class RetryConfig:
    """指数退避重试配置。"""

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0,
                 max_delay: float = 30.0, backoff: float = 2.0):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff = backoff

    def delay(self, attempt: int) -> float:
        return min(self.base_delay * (self.backoff ** attempt), self.max_delay)


class DataCache:
    """API 响应缓存 (线程本地 dict，无跨进程共享)。

    ttl_hours:  缓存有效期 (小时)
    namespace:  缓存命名空间
    """

    def __init__(self, namespace: str, ttl_hours: int = 4,
                 backend: CacheBackend = None):
        self.namespace = namespace
        self.ttl_seconds = int(ttl_hours * 3600)
        self._backend = backend or _backend

    def _key(self, raw_key: str) -> str:
        return f"api:{self.namespace}:{raw_key}"

    def get(self, raw_key: str) -> Any:
        return self._backend.get(self._key(raw_key))

    def set(self, raw_key: str, value: Any):
        self._backend.set(self._key(raw_key), value, self.ttl_seconds)

    def invalidate(self, raw_key: str = None):
        if raw_key:
            self._backend.delete(self._key(raw_key))
        else:
            # 清除 namespace 下所有 key (简化: 只能清除已知 key)
            pass


def with_fallback(*fetchers: Callable):
    """多数据源链式降级装饰器。按顺序尝试 fetcher，直到成功返回非 None 结果。"""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for fetcher in fetchers:
                result = fetcher(*args, **kwargs)
                if result is not None:
                    return result
            return None
        return wrapper
    return decorator
