import asyncio
import contextlib
import json
import logging
import math
import struct
import time
import uuid
from collections.abc import Sequence
from typing import Any

from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.types import Backend

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Local copies carry the expiry of the shared entry (Unix time, 0 for none) in front of the value.
_EXPIRY = struct.Struct("!d")
# A tag invalidation names the keys it deleted, up to this many; beyond, every copy is dropped.
_MAX_ANNOUNCED_KEYS = 1_000
# The invalidation channel is read with a timeout of its own (the client's socket_timeout would end
# every quiet wait with an error), pinged after _PING_AFTER seconds without a message, and given up
# as lost after _LOST_AFTER: a connection that died silently mustn't keep stale copies around.
_READ_TIMEOUT = 1.0
_PING_AFTER = 5.0
_LOST_AFTER = 10.0


class TieredBackend(Backend):
    """Two levels: a small cache in the memory of each process in front of a shared backend.

    A hit in the process skips the network. Each local copy lives at most `local_ttl` seconds and
    never longer than the shared entry, which bounds how stale it can get when another process
    changes the value.

    With a Redis or Valkey shared backend whose client supports pub/sub (RedisBackend,
    ValkeyBackend; not a valkey-py cluster), writes, clears and tag invalidations are announced on
    `invalidation_channel`, and every process drops its copy right away (a tag invalidation names
    the keys it deleted; beyond 1000, every process drops all its copies). Local copies are only
    kept while the process is subscribed: when the subscription drops (and messages may have been
    missed), the local level is emptied and bypassed until it's back. A value read from the shared
    backend while an invalidation arrives isn't kept: it may be the old one.

    Values are kept locally as the shared backend stores them, so signed or encrypted entries stay
    signed or encrypted in memory too.
    """

    def __init__(
        self,
        remote: Backend,
        *,
        local_ttl: int = 5,
        local_max_size: int = 1_000,
        invalidation_channel: str | None = "fastapi_alcazaba:invalidations",
    ) -> None:
        if local_ttl < 1:
            raise ValueError("local_ttl must be at least 1 second")
        self.remote = remote
        self.local = InMemoryBackend(max_size=local_max_size)
        self.local_ttl = local_ttl
        self.supports_locks = remote.supports_locks
        self.supports_tags = remote.supports_tags
        self.supports_counters = remote.supports_counters
        client = getattr(remote, "_client", None)
        self._client = client if hasattr(client, "pubsub") else None
        self.channel = invalidation_channel if self._client is not None else None
        if invalidation_channel and self.channel is None:
            logger.info(
                "%s has no pub/sub: local copies are only bounded by local_ttl (%ss)",
                type(remote).__name__,
                local_ttl,
            )
        self._id = uuid.uuid4().hex
        self._listener: asyncio.Task[None] | None = None
        self._pubsub: Any = None
        self._subscribed = False
        # Each listener belongs to a generation; close() starts a new one, so a listener that
        # survives its cancellation (Python 3.11's wait_for can swallow it) stops by itself.
        self._generation = 0
        # Moves on with every invalidation: a value read from the shared backend while one
        # arrives may be the old one, and isn't kept.
        self._epoch = 0
        self.local_hits = 0
        self.local_misses = 0

    # region Local level

    @property
    def _local_enabled(self) -> bool:
        return self.channel is None or self._subscribed

    async def _keep_locally(self, key: str, value: bytes, ttl: int) -> None:
        if not self._local_enabled:
            return
        expires_at = time.time() + ttl if ttl > 0 else 0.0
        local_expire = min(ttl, self.local_ttl) if ttl > 0 else self.local_ttl
        await self.local.set(key, _EXPIRY.pack(expires_at) + value, local_expire)

    async def _from_local(self, key: str) -> tuple[int, bytes] | None:
        if not self._local_enabled:
            return None
        _, packed = await self.local.get_with_ttl(key)
        if packed is None:
            return None
        (expires_at,) = _EXPIRY.unpack_from(packed)
        value = packed[_EXPIRY.size :]
        if not expires_at:
            return -1, value
        remaining = math.ceil(expires_at - time.time())
        if remaining <= 0:
            await self.local.clear(key=key)
            return None
        return remaining, value

    def _drop_local(self) -> None:
        self._epoch += 1
        self.local = InMemoryBackend(max_size=self.local.max_size)

    # endregion

    # region Invalidation channel

    async def _ensure_listener(self) -> None:
        if self.channel is None:
            return
        if self._listener is None or self._listener.done():
            listener = self._listen(self._generation)
            self._listener = asyncio.get_running_loop().create_task(listener)

    async def _listen(self, generation: int) -> None:
        assert self._client is not None and self.channel is not None  # noqa: S101
        loop = asyncio.get_running_loop()
        backoff = 0.5
        while generation == self._generation:
            pubsub = self._pubsub = self._client.pubsub()
            try:
                await pubsub.subscribe(self.channel)
                heard, pinged = loop.time(), False
                while True:
                    message = await pubsub.get_message(timeout=_READ_TIMEOUT)
                    if generation != self._generation:
                        return
                    now = loop.time()
                    if message is None:
                        if now - heard >= _LOST_AFTER:
                            raise ConnectionError("the invalidation channel stopped answering")
                        if now - heard >= _PING_AFTER and not pinged:
                            await pubsub.ping()
                            pinged = True
                        continue
                    heard, pinged = now, False
                    if message.get("type") == "message":
                        await self._apply(message.get("data"))
                    elif message.get("type") == "subscribe":
                        if self._subscribed:
                            # The client reconnected by itself: messages may have been missed.
                            self._drop_local()
                        self._subscribed = True
                        backoff = 0.5
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Lost the invalidation channel: local copies dropped", exc_info=True)
            finally:
                if generation == self._generation:
                    self._subscribed = False
                    self._drop_local()  # messages may have been missed
                with contextlib.suppress(Exception):
                    await pubsub.aclose()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _apply(self, data: Any) -> None:
        try:
            message = json.loads(data)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict) or message.get("sender") == self._id:
            return
        self._epoch += 1
        keys = message.get("keys", [])
        if (
            message.get("everything") is True
            or not isinstance(keys, list)
            or not all(isinstance(key, str) for key in keys)
            # Tags without keys: fastapi-alcazaba 0.1, during a rolling deploy.
            or ("tags" in message and "keys" not in message)
        ):
            self._drop_local()
            return
        if isinstance(message.get("key"), str):
            keys = [*keys, message["key"]]
        for key in keys:
            await self.local.clear(key=key)
        if isinstance(message.get("namespace"), str):
            await self.local.clear(namespace=message["namespace"])

    async def _announce(self, **message: Any) -> None:
        if self.channel is None or self._client is None:
            return
        try:
            await self._client.publish(self.channel, json.dumps({"sender": self._id, **message}))
        except Exception:
            # The other processes' copies still expire within local_ttl.
            logger.warning("Couldn't announce an invalidation", exc_info=True)

    async def close(self) -> None:
        """Stop listening for invalidations (the next call starts again) and drop the copies."""
        self._generation += 1
        listener, self._listener = self._listener, None
        self._subscribed = False
        self._drop_local()
        if listener is None:
            return
        listener.cancel()
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose()  # also unblocks listen() if the cancel gets lost
        await asyncio.wait({listener}, timeout=2)

    # endregion

    # region Backend

    async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
        await self._ensure_listener()
        local = await self._from_local(key)
        if local is not None:
            self.local_hits += 1
            return local
        self.local_misses += 1
        epoch = self._epoch
        ttl, value = await self.remote.get_with_ttl(key)
        if value is not None and epoch == self._epoch:
            await self._keep_locally(key, value, ttl)
        return ttl, value

    async def get(self, key: str) -> bytes | None:
        return (await self.get_with_ttl(key))[1]

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        await self._ensure_listener()
        epoch = self._epoch
        await self.remote.set(key, value, expire)
        # Another write in the meantime may have overtaken this one in the shared backend.
        overtaken, self._epoch = epoch != self._epoch, self._epoch + 1
        if overtaken:
            await self.local.clear(key=key)
        else:
            await self._keep_locally(key, value, expire or -1)
        await self._announce(key=key)

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        deleted = await self.remote.clear(namespace, key)
        self._epoch += 1
        await self.local.clear(namespace, key)
        if namespace:
            await self._announce(namespace=namespace)
        elif key:
            await self._announce(key=key)
        return deleted

    async def acquire_lock(self, key: str, token: str, ttl: float) -> bool:
        return await self.remote.acquire_lock(key, token, ttl)

    async def release_lock(self, key: str, token: str) -> None:
        await self.remote.release_lock(key, token)

    async def extend_lock(self, key: str, token: str, ttl: float) -> bool:
        return await self.remote.extend_lock(key, token, ttl)

    async def add_tags(self, key: str, tags: Sequence[str], expire: int | None) -> None:
        # Only the shared backend knows every entry's tags (a process that read an entry didn't
        # see them): invalidations name the keys they deleted.
        await self.remote.add_tags(key, tags, expire)

    async def invalidate_tags(self, tags: Sequence[str]) -> int:
        return (await self.invalidate_tags_with_keys(tags))[0]

    async def invalidate_tags_with_keys(self, tags: Sequence[str]) -> tuple[int, list[str] | None]:
        deleted, keys = await self.remote.invalidate_tags_with_keys(tags)
        if keys is None or len(keys) > _MAX_ANNOUNCED_KEYS:
            self._drop_local()
            await self._announce(tags=list(tags), everything=True)
        else:
            self._epoch += 1
            for key in keys:
                await self.local.clear(key=key)
            await self._announce(tags=list(tags), keys=keys)
        return deleted, keys

    # Counters are shared by every process: they're never kept locally.

    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        return await self.remote.incr(key, amount, expire)

    async def get_counter(self, key: str) -> int:
        return await self.remote.get_counter(key)

    async def incr_window(
        self, key: str, previous_key: str, *, amount: int, limit: int, weight: float, expire: int
    ) -> tuple[bool, int, int]:
        return await self.remote.incr_window(
            key, previous_key, amount=amount, limit=limit, weight=weight, expire=expire
        )

    # endregion
