#!/usr/bin/env python3
"""Redis 缓存后端完整测试。需要在 Redis 运行的环境中执行。

用法:
  # 1. 启动 Redis
  redis-server --daemonize yes --port 6379

  # 2. 运行测试
  PYTHONPATH=. .venv/bin/python3 scripts/test_redis_cache.py

  # 3. 停止 Redis (可选)
  redis-cli shutdown
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.cache import (
    get_backend, reset_backend,
    RedisBackend, NoopBackend, CacheBackend,
    RateLimiter, RetryConfig, DataCache, with_fallback,
)
from config.loader import reload

passed = 0
failed = 0

def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")

print("=" * 60)
print("Redis Cache Backend Tests")
print("=" * 60)

# ── Config load ──
print("\n--- Config ---")
cfg = reload()
redis_cfg = cfg.get("cache", {}).get("redis", {})
check("config has cache.redis", bool(redis_cfg))
check("config host=localhost", redis_cfg.get("host") == "localhost")

# ── Redis connection ──
print("\n--- Redis Connection ---")
reset_backend()
backend = get_backend(cfg)
check("backend is RedisBackend", isinstance(backend, RedisBackend))
check("redis ping", backend.ping())

# ── K/V ops ──
print("\n--- K/V Cache ---")
kv_key = "test:kv:1"
backend.delete(kv_key)
check("get miss", backend.get(kv_key) is None)
backend.set(kv_key, b"hello redis", 60)
check("get hit", backend.get(kv_key) == b"hello redis")
backend.delete(kv_key)
check("delete works", backend.get(kv_key) is None)

# ── Rate limit ──
print("\n--- Rate Limit (sliding window) ---")
ns = "test:rate:limit"
w = 60
n = 5
for i in range(n):
    check(f"acquire {i+1}/{n}", backend.rate_limit_acquire(ns, n, w))
check("rate limited (6th call)", not backend.rate_limit_acquire(ns, n, w))

# ── Lock ──
print("\n--- Distributed Lock ---")
lk = "test:lock:1"
backend.lock_release(lk)
check("lock acquire (free)", backend.lock_acquire(lk, 10))
check("lock acquire (held)", not backend.lock_acquire(lk, 10))
backend.lock_release(lk)
check("lock acquire (after release)", backend.lock_acquire(lk, 10))
backend.lock_release(lk)

# ── DataCache ──
print("\n--- DataCache ---")
cache = DataCache("test_data", ttl_hours=1, backend=backend)
cache.invalidate()

val1 = {"pe_ttm": 15.5, "pb": 2.1, "symbols": ["000001", "000002"]}
cache.put("2026-07-01", val1)
got = cache.get("2026-07-01")
check("put/get roundtrip", got == val1)

cache.put("2026-07-02", val1)
cache.invalidate("2026-07-01")
check("invalidate single key", cache.get("2026-07-01") is None)
check("other key still present", cache.get("2026-07-02") == val1)

# ── cached decorator ──
print("\n--- cached Decorator ---")
cache2 = DataCache("test_decorator", ttl_hours=1, backend=backend)
cache2.invalidate()
calls = [0]

@cache2.cached(key_fn=lambda d: d)
def fetch(d):
    calls[0] += 1
    return {"date": d, "count": calls[0]}

r1 = fetch("d1")
r2 = fetch("d1")
r3 = fetch("d2")
check("first call hits source", r1["count"] == 1)
check("second call uses cache", r2["count"] == 1)
check("different key hits source", r3["count"] == 2)

# ── RateLimiter (high-level) ──
print("\n--- RateLimiter (high-level) ---")
limiter = RateLimiter("test_high", calls_per_minute=8, backend=backend)
for i in range(8):
    check(f"limiter acquire {i+1}", limiter.acquire())
check("limiter exhausted", not limiter.acquire())

# ── RetryConfig ──
print("\n--- RetryConfig ---")
retry = RetryConfig(max_retries=2, base_delay=0.01)
attempts = [0]
def flaky():
    attempts[0] += 1
    if attempts[0] < 3:
        raise ConnectionError("transient fail")
    return "recovered"
check("retry recovers", retry.execute(flaky) == "recovered")
check("retry count correct", attempts[0] == 3)

# ── with_fallback ──
print("\n--- with_fallback ---")
chain = with_fallback(
    lambda: (_ for _ in ()).throw(RuntimeError("s1")),
    lambda: (_ for _ in ()).throw(ValueError("s2")),
    lambda: "final",
)
check("fallback chain", chain() == "final")

# ── NoopBackend fallback ──
print("\n--- NoopBackend Fallback ---")
reset_backend()
b2 = get_backend({})  # no config → NoopBackend
check("no config → NoopBackend", isinstance(b2, NoopBackend))
check("noop get is None", b2.get("x") is None)
check("noop ping is False", not b2.ping())
check("noop rate_limit always True", b2.rate_limit_acquire("x", 1, 60))

# ── Cleanup ──
cache.invalidate()
cache2.invalidate()
backend.delete(kv_key)
backend.delete(lk)

# ── Summary ──
print()
print("=" * 60)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
print("=" * 60)
sys.exit(0 if failed == 0 else 1)
