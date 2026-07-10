#!/usr/bin/env python3
"""API 数据拉取缓存层: 限流、重试、Redis 缓存降级。

架构 (ADR 014):
  Redis 仅做 API 去重 + 分布式限流。消费者 (pipeline/web) 从 SQLite 读数据。
  Redis 不可达时降级为 NoopBackend (直连 API + 不限流)。

模块组件:
- CacheBackend (ABC)     — 缓存后端抽象
- RedisBackend           — Redis GET/SET/EXPIRE + SORTED SET sliding window 限流
- NoopBackend            — Redis 不可达时的降级: 无缓存 + 不限流
- RateLimiter            — 分布式限流器 (基于 backend)
- RetryConfig            — 指数退避重试
- DataCache              — API 响应缓存 (基于 backend)
- with_fallback          — 多数据源链式降级

用法:
    from data.cache import get_backend, RateLimiter, DataCache

    backend = get_backend()
    limiter = RateLimiter("tushare_stock_list", calls_per_minute=200, backend=backend)
    cache = DataCache("jq_valuation", ttl_hours=4, backend=backend)
"""
import abc
import functools
import logging
import os
import time
import uuid
from typing import Any, Callable, Optional
from config.constants import _require_cfg

try:
    import msgpack
except ImportError:
    msgpack = None

try:
    import redis
    from redis.exceptions import RedisError
except ImportError:
    redis = None
    RedisError = ConnectionError  # type: ignore

logger = logging.getLogger("quant.data.cache")

# Redis key 前缀 (统一 namespace 隔离)
_KEY_PREFIX = "quant"


# ═══════════════════════════════════════════════════════════
# Backend ABC
# ═══════════════════════════════════════════════════════════

class CacheBackend(abc.ABC):
    """缓存后端抽象。所有数据以 bytes 存储。"""

    @abc.abstractmethod
    def get(self, key: str) -> Optional[bytes]:
        ...

    @abc.abstractmethod
    def set(self, key: str, value: bytes, ttl: int) -> None:
        ...

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        ...

    @abc.abstractmethod
    def rate_limit_acquire(self, namespace: str, max_per_window: int, window_sec: int) -> bool:
        """滑动窗口限流。返回 True 表示允许调用。"""
        ...

    @abc.abstractmethod
    def lock_acquire(self, key: str, ttl: int) -> bool:
        """获取分布式锁。返回 True 表示成功获取。"""
        ...

    @abc.abstractmethod
    def lock_release(self, key: str) -> None:
        ...

    @abc.abstractmethod
    def ping(self) -> bool:
        """健康检查。"""
        ...


# ═══════════════════════════════════════════════════════════
# Redis Backend
# ═══════════════════════════════════════════════════════════

class RedisBackend(CacheBackend):
    """Redis 缓存后端。

    Key schema:
      {prefix}:cache:{namespace}:{key}     — API 响应缓存 (EX)
      {prefix}:lock:{namespace}:{key}      — 同步幂等锁 (NX EX)
      {prefix}:ratelimit:{namespace}       — SORTED SET 滑动窗口限流

    设计决策:
    - decode_responses=False: 存储 msgpack bytes, 避免 str/bytes 混淆
    - hiredis: C 扩展解析器, 减少 CPU 开销
    - connection_pool: 复用 TCP 连接
    """

    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0):
        if redis is None:
            raise ImportError("redis package not installed. Run: pip install redis[hiredis]")
        self._pool = redis.ConnectionPool(
            host=host, port=port, db=db,
            decode_responses=False,
            socket_connect_timeout=_require_cfg("cache.redis.socket_connect_timeout"),
            socket_keepalive=True,
            health_check_interval=30,
        )
        self._client = redis.Redis(connection_pool=self._pool)

    def _k(self, category: str, key: str) -> str:
        return f"{_KEY_PREFIX}:{category}:{key}"

    # -- K/V cache --

    def get(self, key: str) -> Optional[bytes]:
        try:
            return self._client.get(key)
        except RedisError as e:
            logger.warning(f"Redis GET failed ({key[:40]}): {e}")
            return None

    def set(self, key: str, value: bytes, ttl: int) -> None:
        try:
            self._client.setex(key, ttl, value)
        except RedisError as e:
            logger.warning(f"Redis SET failed ({key[:40]}): {e}")

    def delete(self, key: str) -> None:
        try:
            self._client.delete(key)
        except RedisError as e:
            logger.warning(f"Redis DEL failed ({key[:40]}): {e}")

    # -- Rate limit (SORTED SET sliding window) --

    def rate_limit_acquire(self, namespace: str, max_per_window: int, window_sec: int) -> bool:
        """使用 Redis SORTED SET 实现分布式滑动窗口限流。

        每个请求以 uuid + timestamp 作为 member/score 写入 SORTED SET。
        清理窗口外的旧条目后, 统计当前窗口内请求数。
        所有操作在 pipeline 中执行, 单次 RTT。
        """
        key = self._k("ratelimit", namespace)
        now_ms = int(time.time() * 1000)
        window_ms = window_sec * 1000
        cutoff = now_ms - window_ms
        member = f"{uuid.uuid4().hex[:8]}:{now_ms}"
        try:
            pipe = self._client.pipeline()
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zadd(key, {member: now_ms})
            pipe.zcard(key)
            pipe.expire(key, window_sec * 3)  # 3x TTL 防泄漏
            _, _, count, _ = pipe.execute()
            return count <= max_per_window
        except RedisError as e:
            logger.warning(f"Redis rate limit failed ({namespace}): {e}")
            return True  # Redis 挂了不限流 (fail-open)

    # -- Lock --

    def lock_acquire(self, key: str, ttl: int) -> bool:
        try:
            return bool(self._client.set(key, b"1", nx=True, ex=ttl))
        except RedisError:
            return True  # fail-open: Redis 挂了当做获取锁成功

    def lock_release(self, key: str) -> None:
        try:
            self._client.delete(key)
        except RedisError:
            pass

    # -- Health --

    def ping(self) -> bool:
        try:
            return self._client.ping()
        except RedisError:
            return False


# ═══════════════════════════════════════════════════════════
# Noop Backend (Redis 不可达时降级)
# ═══════════════════════════════════════════════════════════

class NoopBackend(CacheBackend):
    """无操作缓存后端 — Redis 不可达时的降级。

    - 缓存永远 miss (不存储)
    - 限流永远放行 (不限流)
    - 锁永远成功 (不阻塞)

    Fail-open 策略: 宁可重复调用 API, 也不阻塞数据同步。
    """

    def get(self, key: str) -> Optional[bytes]:
        return None

    def set(self, key: str, value: bytes, ttl: int) -> None:
        pass

    def delete(self, key: str) -> None:
        pass

    def rate_limit_acquire(self, namespace: str, max_per_window: int, window_sec: int) -> bool:
        return True

    def lock_acquire(self, key: str, ttl: int) -> bool:
        return True

    def lock_release(self, key: str) -> None:
        pass

    def ping(self) -> bool:
        return False


# ═══════════════════════════════════════════════════════════
# Backend factory
# ═══════════════════════════════════════════════════════════

_backend: Optional[CacheBackend] = None


def get_backend(config: dict = None) -> CacheBackend:
    """获取缓存后端实例 (单例)。

    优先级:
    1. 已缓存的实例 (ping 通过)
    2. config 中的 Redis 配置 → 尝试连接
    3. 连接失败 / 未配置 → NoopBackend

    缓存后端实例后, 定期 ping 检测: 如果 Redis 恢复, 下次调用返回 RedisBackend。
    """
    global _backend
    if _backend is not None:
        if isinstance(_backend, NoopBackend):
            # 尝试恢复
            if config:
                try:
                    redis_cfg = config.get("cache", {}).get("redis", {})
                    if redis_cfg:
                        b = RedisBackend(**redis_cfg)
                        if b.ping():
                            logger.info("Redis recovered, switching to RedisBackend")
                            _backend = b
                            return _backend
                except Exception:
                    pass
        else:
            if _backend.ping():
                return _backend
            # Redis 挂了, 降级
            logger.warning("Redis lost, falling back to NoopBackend")
            _backend = NoopBackend()
            return _backend

    if config:
        redis_cfg = config.get("cache", {}).get("redis", {})
        if redis_cfg:
            try:
                b = RedisBackend(**{k: v for k, v in redis_cfg.items()
                                    if k in ("host", "port", "db")})
                if b.ping():
                    logger.info(f"Redis connected {redis_cfg.get('host', 'localhost')}:{redis_cfg.get('port', 6379)}")
                    _backend = b
                    return _backend
                logger.warning("Redis configured but not reachable, using NoopBackend")
            except Exception as e:
                logger.warning(f"Redis init failed ({e}), using NoopBackend")
    else:
        logger.info("No cache config, using NoopBackend")

    _backend = NoopBackend()
    return _backend


def reset_backend():
    """重置后端单例 (测试用)。"""
    global _backend
    _backend = None


# ═══════════════════════════════════════════════════════════
# Rate Limiter — 分布式限流器
# ═══════════════════════════════════════════════════════════

class RateLimiter:
    """分布式限流器 — 基于 Redis SORTED SET sliding window。

    calls_per_minute: 每分钟允许的调用次数
    namespace:        API 标识 (如 "tushare_stock_list"), 用于 Redis key 隔离
    window_sec:       滑动窗口大小 (秒), 默认 60
    backend:          CacheBackend 实例

    用法:
        limiter = RateLimiter("tushare_stock_list", 200, backend=backend)

        @limiter
        def fetch():
            return api.query()

        if limiter.acquire():
            fetch()
    """

    def __init__(self, namespace: str, calls_per_minute: int = 30,
                 window_sec: int = 60, backend: CacheBackend = None):
        self.namespace = f"rl:{namespace}"
        self.max_per_window = calls_per_minute
        self.window_sec = window_sec
        self.backend = backend or get_backend()
        self._local_burst = min(calls_per_minute, 10)  # 降级用: 线程内令牌桶, 上限10防止本地无界消费 API
        self._local_tokens = self._local_burst

    def acquire(self) -> bool:
        """尝试获取调用许可。成功返回 True。不阻塞。"""
        if self.backend.rate_limit_acquire(self.namespace, self.max_per_window, self.window_sec):
            return True
        # 降级: 线程内令牌桶 (Redis 不可达时)
        if isinstance(self.backend, NoopBackend) and self._local_tokens > 0:
            self._local_tokens -= 1
            return True
        return False

    def wait(self, timeout: float = 30.0) -> bool:
        """等待获取许可。超时返回 False。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.acquire():
                return True
            time.sleep(_require_cfg("cache.retry_delay"))
        return False

    def __call__(self, fn):
        """装饰器: 自动等待许可后调用函数。"""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            self.wait()
            return fn(*args, **kwargs)
        return wrapper

    def reset_local(self):
        """重置本地令牌桶。"""
        self._local_tokens = self._local_burst


# ═══════════════════════════════════════════════════════════
# Retry — 指数退避
# ═══════════════════════════════════════════════════════════

class RetryConfig:
    """重试配置。

    max_retries:     最大重试次数
    base_delay:      首次重试等待秒数
    max_delay:       最大等待秒数
    backoff_factor:  退避因子
    retry_on:        触发重试的异常类型元组
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff_factor: float = 2.0,
        retry_on: tuple = None,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.retry_on = retry_on or (ConnectionError, TimeoutError, OSError)

    def execute(self, fn: Callable, *args, **kwargs) -> Any:
        """执行函数，失败时指数退避重试。"""
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except self.retry_on as e:
                last_exc = e
                if attempt < self.max_retries:
                    delay = min(
                        self.base_delay * (self.backoff_factor ** attempt),
                        self.max_delay,
                    )
                    logger.debug(f"retry {attempt + 1}/{self.max_retries} in {delay:.1f}s: {e}")
                    time.sleep(delay)
        raise last_exc


# ═══════════════════════════════════════════════════════════
# DataCache — API 响应缓存
# ═══════════════════════════════════════════════════════════

class DataCache:
    """API 响应缓存 — 基于 CacheBackend。

    namespace:  缓存命名空间 (如 "jq_valuation", "stock_list")
    ttl_hours:  缓存有效期 (小时), Redis EX
    backend:    CacheBackend 实例

    缓存 key 格式: {namespace}:{user_key}

    序列化: msgpack (已安装) → JSON (回退)
    """

    def __init__(self, namespace: str, ttl_hours: float = 4.0,
                 backend: CacheBackend = None):
        self.namespace = namespace
        self.ttl = int(ttl_hours * 3600)
        self.backend = backend or get_backend()
        self._use_msgpack = msgpack is not None

    def _full_key(self, key: str) -> str:
        return f"{_KEY_PREFIX}:cache:{self.namespace}:{key}"

    def get(self, key: str) -> Optional[Any]:
        """读取缓存。返回反序列化后的 Python 对象或 None。"""
        data = self.backend.get(self._full_key(key))
        if data is None:
            return None
        try:
            if self._use_msgpack:
                return msgpack.unpackb(data)
            import json
            return json.loads(data)
        except Exception as e:
            logger.warning(f"cache deserialize failed ({self.namespace}/{key[:20]}): {e}")
            return None

    def put(self, key: str, data: Any):
        """写入缓存。自动序列化。"""
        try:
            if self._use_msgpack:
                val = msgpack.packb(data, default=_msgpack_default)
            else:
                import json
                val = json.dumps(data, default=str).encode()
            self.backend.set(self._full_key(key), val, self.ttl)
        except Exception as e:
            logger.warning(f"cache serialize failed ({self.namespace}/{key[:20]}): {e}")

    def invalidate(self, key: str = None):
        """使缓存失效。key=None 时删除所有同 namespace 的 key (Redis: SCAN + DEL)。"""
        if key:
            self.backend.delete(self._full_key(key))
        elif isinstance(self.backend, RedisBackend):
            pattern = f"{_KEY_PREFIX}:cache:{self.namespace}:*"
            try:
                cursor = 0
                while True:
                    cursor, keys = self.backend._client.scan(cursor, match=pattern, count=100)
                    if keys:
                        self.backend._client.delete(*keys)
                    if cursor == 0:
                        break
            except RedisError:
                pass

    def cached(self, key_fn: Callable = None):
        """装饰器: 缓存函数返回值 (cache-aside 模式)。

        key_fn(*args, **kwargs) → str: 生成缓存 key。
        未指定时使用 args[0] 的字符串表示。

        行为:
        1. 缓存命中 → 返回缓存
        2. 缓存未命中 → 调用源函数 → 写入缓存 → 返回
        3. 源函数异常 → 抛出 (不拦截)
        """
        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                k = key_fn(*args, **kwargs) if key_fn else str(args[0]) if args else "default"
                cached_val = self.get(k)
                if cached_val is not None:
                    return cached_val
                result = fn(*args, **kwargs)
                self.put(k, result)
                return result
            return wrapper
        return decorator


# ═══════════════════════════════════════════════════════════
# Fallback — 多数据源链式降级
# ═══════════════════════════════════════════════════════════

def with_fallback(*fetchers: Callable, on_fallback: Callable = None):
    """创建一个链式降级 fetcher。

    依次尝试 fetchers, 任一成功即返回。全部失败时抛最后一个异常。

    用法:
        fetch = with_fallback(
            fetch_from_tushare,
            fetch_from_akshare,
            on_fallback=lambda src: logger.warning(f"falling back from {src}")
        )
        data = fetch(date="2026-07-01")
    """
    def execute(*args, **kwargs):
        last_exc = None
        for i, fetcher in enumerate(fetchers):
            try:
                return fetcher(*args, **kwargs)
            except Exception as e:
                last_exc = e
                src_name = getattr(fetcher, "__name__", str(fetcher))
                logger.info(f"data source [{src_name}] failed: {e}")
                if on_fallback:
                    on_fallback(src_name)
        raise last_exc
    return execute


def _msgpack_default(obj):
    """msgpack 序列化 numpy/pandas 类型的 default handler。"""
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict(orient="records")
    if hasattr(obj, "tolist"):
        return obj.tolist()
    if hasattr(obj, "item"):
        return obj.item()
    raise TypeError(f"Unserializable type: {type(obj)}")
