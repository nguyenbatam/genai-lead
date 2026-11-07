"""Project admission. Redis admits the window. Postgres keeps the high-water mark."""

from __future__ import annotations

from typing import Protocol

from control.store import WINDOW_MS, Store

# Admit only when under the limit. Denied calls do not inflate the counter.
_ADMIT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local count = redis.call('GET', key)
if count == false then
  redis.call('SET', key, 1, 'EX', ttl)
  return 1
end
count = tonumber(count)
if count >= limit then
  return 0
end
count = redis.call('INCR', key)
if count == 1 then
  redis.call('EXPIRE', key, ttl)
end
return count
"""


class Quota(Protocol):
    def consume(self, project_id: str, now_ms: int) -> bool: ...

    def ping(self) -> bool: ...


class MemoryQuota:
    def __init__(self, store: Store) -> None:
        self.store = store

    def consume(self, project_id: str, now_ms: int) -> bool:
        return self.store.consume_request(project_id, now_ms)

    def ping(self) -> bool:
        return True


class RedisQuota:
    def __init__(self, url: str, store: Store, client: object | None = None) -> None:
        self.store = store
        self._sha: str | None = None
        if client is None:
            import redis

            self.client = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
        else:
            self.client = client

    def consume(self, project_id: str, now_ms: int) -> bool:
        """Redis decides. Postgres records the high-water mark and is the fallback."""
        import redis

        try:
            limit, _count = self.store.project_limit(project_id)
        except KeyError:
            return False
        window = now_ms // WINDOW_MS
        key = f"quota:{project_id}:{window}"
        try:
            admitted = int(self._eval_admit(key, limit))
        except (OSError, redis.RedisError, TypeError, ValueError):
            return self.store.consume_request(project_id, now_ms)
        if admitted <= 0:
            self._mirror(project_id, window * WINDOW_MS, limit)
            return False
        self._mirror(project_id, window * WINDOW_MS, admitted)
        return True

    def _eval_admit(self, key: str, limit: int) -> int:
        import redis

        args = (key, str(limit), "120")
        load = getattr(self.client, "script_load", None)
        evalsha = getattr(self.client, "evalsha", None)
        if load is None or evalsha is None:
            return int(self.client.eval(_ADMIT, 1, *args))  # type: ignore[misc]
        try:
            if self._sha is None:
                self._sha = str(load(_ADMIT))
            return int(evalsha(self._sha, 1, *args))
        except redis.exceptions.NoScriptError:
            self._sha = str(load(_ADMIT))
            return int(evalsha(self._sha, 1, *args))

    def _mirror(self, project_id: str, window_start_ms: int, count: int) -> None:
        try:
            self.store.note_window(project_id, window_start_ms, count)
        except (OSError, RuntimeError):
            return

    def ping(self) -> bool:
        import redis

        try:
            return bool(self.client.ping())
        except (OSError, redis.RedisError):
            return False
