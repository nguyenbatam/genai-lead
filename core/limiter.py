"""Per-project in-flight caps. Rate quota is separate; this bounds overlap."""

from __future__ import annotations

import threading
from typing import Protocol

DEFAULT_CHAT_LIMIT = 2
DEFAULT_RUN_LIMIT = 2
# Crash safety: a stuck hold expires so one process death cannot pin a project forever.
_HOLD_TTL_S = 120


class Limiter(Protocol):
    def acquire(self, project_id: str, kind: str, limit: int) -> bool: ...

    def release(self, project_id: str, kind: str) -> None: ...

    def ping(self) -> bool: ...


class MemoryLimiter:
    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def acquire(self, project_id: str, kind: str, limit: int) -> bool:
        if limit < 1:
            return False
        key = (project_id, kind)
        with self._lock:
            count = self._counts.get(key, 0)
            if count >= limit:
                return False
            self._counts[key] = count + 1
            return True

    def release(self, project_id: str, kind: str) -> None:
        key = (project_id, kind)
        with self._lock:
            count = self._counts.get(key, 0)
            if count <= 1:
                self._counts.pop(key, None)
                return
            self._counts[key] = count - 1

    def ping(self) -> bool:
        return True


_ACQUIRE = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local count = tonumber(redis.call('GET', key) or '0')
if count >= limit then
  return 0
end
count = redis.call('INCR', key)
if count == 1 then
  redis.call('EXPIRE', key, ttl)
end
return 1
"""

_RELEASE = """
local key = KEYS[1]
local count = tonumber(redis.call('GET', key) or '0')
if count <= 1 then
  redis.call('DEL', key)
  return 0
end
return redis.call('DECR', key)
"""


class RedisLimiter:
    def __init__(self, url: str, client: object | None = None) -> None:
        if client is None:
            import redis

            self.client = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
        else:
            self.client = client
        self._fallback = MemoryLimiter()

    def acquire(self, project_id: str, kind: str, limit: int) -> bool:
        if limit < 1:
            return False
        import redis

        key = f"inflight:{kind}:{project_id}"
        try:
            admitted = int(
                self.client.eval(_ACQUIRE, 1, key, str(limit), str(_HOLD_TTL_S))  # type: ignore[misc]
            )
        except (OSError, redis.RedisError, TypeError, ValueError):
            return self._fallback.acquire(project_id, kind, limit)
        return admitted > 0

    def release(self, project_id: str, kind: str) -> None:
        import redis

        key = f"inflight:{kind}:{project_id}"
        try:
            self.client.eval(_RELEASE, 1, key)  # type: ignore[misc]
        except (OSError, redis.RedisError, TypeError, ValueError):
            self._fallback.release(project_id, kind)

    def ping(self) -> bool:
        import redis

        try:
            return bool(self.client.ping())
        except (OSError, redis.RedisError):
            return False
