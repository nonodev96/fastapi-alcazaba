import hashlib
import time

from aiomcache import Client

from fastapi_alcazaba.types import Backend

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


class MemcachedBackend(Backend):
    def __init__(self, mcache: Client) -> None:
        self.mcache = mcache

    async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
        value = await self.get(key)
        # memcached can't tell the remaining time to live (fastapi-cache2 always said 3600).
        return (0, None) if value is None else (-1, value)

    async def get(self, key: str) -> bytes | None:
        value: bytes | None = await self.mcache.get(memcached_key(key))
        return value

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        exptime = expire or 0
        if exptime > _MAX_RELATIVE_EXPTIME:
            exptime += int(time.time())
        await self.mcache.set(memcached_key(key), value, exptime=exptime)

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        if namespace:
            # flush_all (upstream PR #477) would also wipe every other application's data.
            raise NotImplementedError(
                "memcached can't list the keys of a namespace; delete them one by one with `key`"
            )
        if key:
            return int(await self.mcache.delete(memcached_key(key)))
        return 0
