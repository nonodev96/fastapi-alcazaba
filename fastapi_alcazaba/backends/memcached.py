import hashlib
import logging
import math
import time
from collections.abc import Sequence

from aiomcache import Client
from aiomcache.exceptions import ClientException

from fastapi_alcazaba.backends._generations import (
    Stamp,
    alive,
    check_depth,
    encode_stamp,
    namespace_generation,
    namespaces,
    new_token,
    parse_stamp,
    tag_generation,
)
from fastapi_alcazaba.backends.base import Backend, is_state_key

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Beyond 30 days memcached reads exptime as a Unix timestamp, not as seconds from now.
_MAX_RELATIVE_EXPTIME = 30 * 24 * 3600
# A Unix time before the server started: memcached takes it as already expired (aiomcache refuses
# a negative exptime, memcached's other way to say so).
_EXPIRED = _MAX_RELATIVE_EXPTIME + 1
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


def _lock_exptime(ttl: float) -> int:
    """memcached counts whole seconds on a clock that ticks once a second: an item set for N
    seconds lasts between N - 1 and N. A lock never lasts less than `ttl`."""
    return _exptime(math.ceil(ttl) + 1)


class MemcachedBackend(Backend):
    """memcached through aiomcache.

    Cached responses are stored with a stamp: their expiry, so hits know their remaining lifetime,
    and the generations of their namespaces and tags (see `_generations`), so that
    `clear(namespace=...)` and `invalidate_tags` hide them without listing keys. memcached can't
    count them: both return 0. State (`Cache.state_key`: locks, counters, sessions...) is stored as
    is.

    Locks are taken with `add`, and released or extended with `gets` + `cas`, so only their holder
    can. memcached keeps time in whole seconds: a lock lasts up to two seconds more than asked,
    never less.

    :param max_value_size: the largest value stored, stamp included: memcached's item size (1 MiB
        unless the server runs with `-I`) less room for the key and memcached's own header.
    """

    supports_locks = True
    supports_tags = True
    supports_counters = True

    def __init__(self, mcache: Client, *, max_value_size: int = 1024 * 1024 - 512) -> None:
        if max_value_size < 1:
            raise ValueError("max_value_size must be at least 1 byte")
        self.mcache = mcache
        self.max_value_size = max_value_size

    async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
        if is_state_key(key):
            value = await self.mcache.get(memcached_key(key))
            return (0, None) if value is None else (-1, value)
        required = [namespace_generation(namespace) for namespace in namespaces(key)]
        found = await self._get_many([key, *required])
        data = found.pop(key)
        if data is None:
            return 0, None
        try:
            parsed = parse_stamp(data)
        except ValueError:
            logger.warning("Ignoring the unreadable entry %r", key, exc_info=True)
            return 0, None
        if parsed is None:
            # Written by an earlier version: whether a clear hid it since can't be told.
            return 0, None
        stamp, value = parsed
        if others := [name for name in stamp.generations if name not in found]:
            found.update(await self._get_many(others))
        current = {name: token.decode() if token else None for name, token in found.items()}
        if not alive(stamp, current, required):
            return 0, None
        if not stamp.expires_at:
            return -1, value
        remaining = math.ceil(stamp.expires_at - time.time())
        return (remaining, value) if remaining > 0 else (0, None)

    async def get(self, key: str) -> bytes | None:
        return (await self.get_with_ttl(key))[1]

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        if not is_state_key(key):
            names = [namespace_generation(namespace) for namespace in namespaces(key)]
            expires_at = int(time.time()) + expire if expire else 0
            value = encode_stamp(Stamp(expires_at, await self._generations(names)), value)
        self.check_size(key, value)
        await self.mcache.set(memcached_key(key), value, exptime=_exptime(expire))

    async def _get_many(self, keys: Sequence[str]) -> dict[str, bytes | None]:
        values = await self.mcache.multi_get(*(memcached_key(key) for key in keys))
        return dict(zip(keys, values, strict=True))

    async def _generations(self, names: Sequence[str]) -> dict[str, str]:
        """The current token of each generation, creating those missing: an entry always records a
        real token, so a generation memcached evicts makes its entries miss, and never brings back
        those a clear hid."""
        tokens: dict[str, str] = {}
        for name, token in (await self._get_many(names) if names else {}).items():
            raw = memcached_key(name)
            while token is None:
                created = new_token().encode()
                # Unless another client created it since the read: then theirs.
                added = await self.mcache.add(raw, created)
                token = created if added else await self.mcache.get(raw)
            tokens[name] = token.decode()
        return tokens

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

    async def get_counter(self, key: str) -> int:
        # memcached's own numbers, never stamped, whatever the key.
        value = await self.mcache.get(memcached_key(key))
        return int(value) if value else 0

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
        """Hide every entry of `namespace` and return 0 (memcached can't count them), or delete
        the entry `key`."""
        if namespace is not None:
            check_depth(namespace)
            await self._renew(namespace_generation(namespace))
            return 0
        if key:
            return int(await self.mcache.delete(memcached_key(key)))
        return 0

    async def _renew(self, name: str) -> None:
        await self.mcache.set(memcached_key(name), new_token().encode())

    async def acquire_lock(self, key: str, token: str, ttl: float) -> bool:
        raw = memcached_key(key)
        return bool(await self.mcache.add(raw, token.encode(), exptime=_lock_exptime(ttl)))

    async def release_lock(self, key: str, token: str) -> None:
        raw = memcached_key(key)
        value, cas = await self.mcache.gets(raw)
        if value == token.encode() and cas is not None:
            # Only if nobody wrote since the gets; expired at once, so the next add takes it.
            await self.mcache.cas(raw, b"", cas, exptime=_EXPIRED)

    async def extend_lock(self, key: str, token: str, ttl: float) -> bool:
        raw = memcached_key(key)
        value, cas = await self.mcache.gets(raw)
        if value != token.encode() or cas is None:
            return False
        return bool(await self.mcache.cas(raw, value, cas, exptime=_lock_exptime(ttl)))

    async def add_tags(self, key: str, tags: Sequence[str], expire: int | None) -> None:
        raw = memcached_key(key)
        data, cas = await self.mcache.gets(raw)
        if data is None or cas is None:
            return
        try:
            parsed = parse_stamp(data)
        except ValueError:
            return
        if parsed is None:
            return
        stamp, value = parsed
        tokens = await self._generations([tag_generation(tag) for tag in tags])
        tagged = Stamp(stamp.expires_at, {**stamp.generations, **tokens})
        # Only if nobody wrote since the gets: a newer value is tagged by its own writer. The
        # expiry, a Unix time, is the entry's own.
        await self.mcache.cas(raw, encode_stamp(tagged, value), cas, exptime=stamp.expires_at)

    async def invalidate_tags(self, tags: Sequence[str]) -> int:
        """Hide every entry of `tags` and return 0 (memcached can't count them)."""
        for tag in tags:
            await self._renew(tag_generation(tag))
        return 0
