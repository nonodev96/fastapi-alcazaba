import hashlib
import time

from aiomcache import Client
from aiomcache.exceptions import ClientException

from fastapi_alcazaba.backends.base import Backend

# Beyond 30 days memcached reads exptime as a Unix timestamp, not as seconds from now.
_MAX_RELATIVE_EXPTIME = 30 * 24 * 3600
_MAX_KEY_LENGTH = 250


def memcached_key(key: str) -> bytes:
    """memcached keys are at most 250 bytes of printable ASCII without spaces: anything else is
    replaced by its SHA-256 (the keys Alcazaba builds always fit)."""
    raw = key.encode()
    if len(raw) <= _MAX_KEY_LENGTH and all(33 <= byte <= 126 for byte in raw):
        return raw
    return b"fastapi_alcazaba:sha256:" + hashlib.sha256(raw).hexdigest().encode()


def _exptime(expire: int | None) -> int:
    exptime = expire or 0
    if exptime > _MAX_RELATIVE_EXPTIME:
        exptime += int(time.time())
    return exptime


class MemcachedBackend(Backend):
    supports_counters = True

    def __init__(self, mcache: Client) -> None:
        self.mcache = mcache

    async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
        value = await self.get(key)
        # memcached can't tell the remaining time to live.
        return (0, None) if value is None else (-1, value)

    async def get(self, key: str) -> bytes | None:
        value: bytes | None = await self.mcache.get(memcached_key(key))
        return value

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        await self.mcache.set(memcached_key(key), value, exptime=_exptime(expire))

    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        """memcached counters don't go below 0."""
        raw = memcached_key(key)
        while True:
            if (value := await self._change(raw, amount)) is not None:
                return value
            # No counter: create it, unless another client did since our incr.
            initial = max(amount, 0)
            if await self.mcache.add(raw, str(initial).encode(), exptime=_exptime(expire)):
                return initial

    async def _change(self, raw: bytes, amount: int) -> int | None:
        """The counter's new value; None when there is no counter."""
        try:
            if amount >= 0:
                value: int | None = await self.mcache.incr(raw, amount)
            else:
                value = await self.mcache.decr(raw, -amount)
        except ClientException as exc:
            # aiomcache raises for a missing key, though it documents returning None; the
            # server's reply only survives in the message.
            if "NOT_FOUND" in str(exc):
                return None
            raise
        return value

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        if namespace:
            # flush_all would also wipe every other application's data.
            raise NotImplementedError(
                "memcached can't list the keys of a namespace; delete them one by one with `key`"
            )
        if key:
            return int(await self.mcache.delete(memcached_key(key)))
        return 0
