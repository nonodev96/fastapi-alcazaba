"""Shared implementation of the Redis and Valkey backends: their clients have the same API."""

import re
import secrets
from collections.abc import Iterable, Sequence
from typing import Any

from fastapi_alcazaba.backends.base import Backend

_GLOB_SPECIAL = re.compile(r"([\\*?\[\]])")

# Keys and values always travel as KEYS/ARGV, never inside the script source.
RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

# KEYS[1]: lock; ARGV[1]: token; ARGV[2]: new expiry in milliseconds.
EXTEND_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

# KEYS[1]: tag set; ARGV[1]: entry key; ARGV[2]: its expiry in seconds, 0 for none. The set lives as
# long as its longest-lived entry: it stays persistent once an entry without expiry joins.
ADD_TAG_SCRIPT = """
local existed = redis.call('EXISTS', KEYS[1]) == 1
redis.call('SADD', KEYS[1], ARGV[1])
local expire = tonumber(ARGV[2])
if expire == 0 then
    redis.call('PERSIST', KEYS[1])
elseif not existed then
    redis.call('EXPIRE', KEYS[1], expire)
else
    local current = redis.call('TTL', KEYS[1])
    if current ~= -1 and current < expire then
        redis.call('EXPIRE', KEYS[1], expire)
    end
end
return 1
"""

# KEYS[1]: counter; ARGV[1]: amount; ARGV[2]: expiry in seconds, 0 for none. The expiry is set
# whenever the counter has none, so a counter can't be left without one.
INCR_SCRIPT = """
local value = redis.call('INCRBY', KEYS[1], ARGV[1])
local expire = tonumber(ARGV[2])
if expire > 0 and redis.call('TTL', KEYS[1]) == -1 then
    redis.call('EXPIRE', KEYS[1], expire)
end
return value
"""

# The rate limiter's sliding window in one round trip. KEYS[1]: counter of the current window;
# KEYS[2]: of the previous one (same hash slot: the limiter's keys carry a {hash tag}). ARGV[1]:
# amount; ARGV[2]: limit; ARGV[3]: weight of the previous window; ARGV[4]: expiry in seconds.
INCR_WINDOW_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local previous = tonumber(redis.call('GET', KEYS[2]) or '0')
local amount = tonumber(ARGV[1])
if previous * tonumber(ARGV[3]) + current + amount > tonumber(ARGV[2]) then
    return {0, current, previous}
end
current = redis.call('INCRBY', KEYS[1], amount)
if redis.call('TTL', KEYS[1]) == -1 then
    redis.call('EXPIRE', KEYS[1], ARGV[4])
end
return {1, current, previous}
"""


def window_args(amount: int, limit: int, weight: float, expire: int) -> list[str]:
    """ARGV of INCR_WINDOW_SCRIPT. repr() gives the shortest text that reads back as the same
    float, so Lua compares exactly what Python would."""
    return [str(amount), str(limit), repr(weight), str(expire)]


def escape_glob(text: str) -> str:
    """Escape the metacharacters of a SCAN MATCH pattern, so that a namespace like "a*" or "a[b]"
    only matches itself."""
    return _GLOB_SPECIAL.sub(r"\\\1", text)


def pending_tag_key(tag: str) -> str:
    """Where a tag set is moved while it's invalidated. Same hash slot as the tag in a cluster:
    the tag keys carry a {hash tag}."""
    return f"{tag}:invalidating:{secrets.token_hex(8)}"


def key_names(keys: Iterable[bytes | str]) -> list[str]:
    """Keys as the client returns them (bytes, or str with decode_responses) as str."""
    return [key.decode(errors="replace") if isinstance(key, bytes) else key for key in keys]


def is_missing_key_error(exc: Exception) -> bool:
    return "no such key" in str(exc).lower()


class RedisLikeBackend(Backend):
    """Backend for redis-py style clients (standalone or cluster).

    `clear(namespace=...)` walks the namespace with SCAN and deletes in batches with UNLINK. Never
    KEYS, which blocks the server while it walks every key, and never a Lua script with the
    namespace in its source: a namespace coming from user input could run arbitrary commands
    (e.g. "x') redis.call('FLUSHALL') --").
    """

    supports_locks = True
    supports_tags = True
    supports_counters = True

    def __init__(self, client: Any, *, is_cluster: bool, scan_count: int = 500) -> None:
        if scan_count < 1:
            raise ValueError("scan_count must be at least 1")
        self._client = client
        self.is_cluster = is_cluster
        self.scan_count = scan_count

    @property
    def client(self) -> Any:
        """The client the backend talks through."""
        return self._client

    async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
        async with self._client.pipeline(transaction=not self.is_cluster) as pipe:
            ttl, value = await pipe.ttl(key).get(key).execute()
        if value is None:
            return 0, None
        return int(ttl), value

    async def get(self, key: str) -> bytes | None:
        value: bytes | None = await self._client.get(key)
        return value

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        await self._client.set(key, value, ex=expire or None)

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        if namespace:
            return await self._delete_matching(f"{escape_glob(namespace)}:*")
        if key:
            return int(await self._client.delete(key))
        return 0

    async def acquire_lock(self, key: str, token: str, ttl: float) -> bool:
        return bool(await self._client.set(key, token, nx=True, px=max(1, int(ttl * 1000))))

    async def release_lock(self, key: str, token: str) -> None:
        await self._client.eval(RELEASE_LOCK_SCRIPT, 1, key, token)

    async def extend_lock(self, key: str, token: str, ttl: float) -> bool:
        milliseconds = str(max(1, int(ttl * 1000)))
        return bool(await self._client.eval(EXTEND_LOCK_SCRIPT, 1, key, token, milliseconds))

    async def add_tags(self, key: str, tags: Sequence[str], expire: int | None) -> None:
        for tag in tags:
            await self._client.eval(ADD_TAG_SCRIPT, 1, tag, key, str(expire or 0))

    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        return int(await self._client.eval(INCR_SCRIPT, 1, key, str(amount), str(expire or 0)))

    async def incr_window(
        self,
        key: str,
        previous_key: str,
        *,
        amount: int,
        limit: int,
        weight: float,
        expire: int,
    ) -> tuple[bool, int, int]:
        args = window_args(amount, limit, weight, expire)
        added, current, previous = await self._client.eval(
            INCR_WINDOW_SCRIPT, 2, key, previous_key, *args
        )
        return bool(added), int(current), int(previous)

    async def invalidate_tags(self, tags: Sequence[str]) -> int:
        return (await self.invalidate_tags_with_keys(tags))[0]

    async def invalidate_tags_with_keys(self, tags: Sequence[str]) -> tuple[int, list[str] | None]:
        deleted, keys = 0, []
        for tag in tags:
            # Moving the set first means that entries tagged while this runs land in a new set and
            # aren't lost; they belong to the next invalidation.
            pending = pending_tag_key(tag)
            try:
                await self._client.rename(tag, pending)
            except Exception as exc:
                if not is_missing_key_error(exc):
                    raise
                continue
            members = list(await self._client.smembers(pending))
            if members:
                deleted += await self._unlink(members)
                keys.extend(key_names(members))
            await self._client.unlink(pending)
        return deleted, keys

    async def _delete_matching(self, pattern: str) -> int:
        deleted = 0
        batch: list[bytes] = []
        async for found in self._client.scan_iter(match=pattern, count=self.scan_count):
            batch.append(found)
            if len(batch) >= self.scan_count:
                deleted += await self._unlink(batch)
                batch = []
        if batch:
            deleted += await self._unlink(batch)
        return deleted

    async def _unlink(self, keys: list[bytes]) -> int:
        # The cluster clients of redis-py and valkey-py split a multi-key UNLINK by hash slot.
        return int(await self._client.unlink(*keys))
