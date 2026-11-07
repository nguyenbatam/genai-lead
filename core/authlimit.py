"""Failed API-key attempt limiter. In-process for unit tests; Redis when shared."""

from __future__ import annotations

import threading
import time


class AuthLimiter:
    """Block a key after too many 401s in a window. Fail-open if Redis is down."""

    def __init__(self, limit: int = 20, window_s: int = 60, client: object | None = None) -> None:
        self.limit = limit
        self.window_s = window_s
        self.client = client
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if not key:
            return True
        if self.client is not None:
            return self._redis_allow(key)
        return self._memory_allow(key)

    def fail(self, key: str) -> None:
        if not key:
            return
        if self.client is not None:
            self._redis_fail(key)
            return
        with self._lock:
            now = time.monotonic()
            bucket = [stamp for stamp in self._hits.get(key, []) if now - stamp < self.window_s]
            bucket.append(now)
            self._hits[key] = bucket

    def _memory_allow(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            bucket = [stamp for stamp in self._hits.get(key, []) if now - stamp < self.window_s]
            self._hits[key] = bucket
            return len(bucket) < self.limit

    def _redis_allow(self, key: str) -> bool:
        import redis

        try:
            count = int(self.client.get(f"authfail:{key}") or 0)
        except (OSError, redis.RedisError, TypeError, ValueError):
            return True
        return count < self.limit

    def _redis_fail(self, key: str) -> None:
        import redis

        try:
            pipe = self.client.pipeline()
            pipe.incr(f"authfail:{key}")
            pipe.expire(f"authfail:{key}", self.window_s)
            pipe.execute()
        except (OSError, redis.RedisError):
            return
