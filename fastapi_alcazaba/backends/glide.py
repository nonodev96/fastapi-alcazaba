from collections.abc import Sequence

from glide import (
    Batch,
    ClusterBatch,
    ClusterScanCursor,
    ConditionalChange,
    ExpirySet,
    ExpiryType,
    GlideClient,
    GlideClusterClient,
    Script,
)

from fastapi_alcazaba.backends._redis_common import (
    ADD_TAG_SCRIPT,
    EXTEND_LOCK_SCRIPT,
    INCR_SCRIPT,
    INCR_WINDOW_SCRIPT,
    RELEASE_LOCK_SCRIPT,
    escape_glob,
    is_missing_key_error,
    key_names,
    pending_tag_key,
    window_args,
)
from fastapi_alcazaba.backends.base import Backend

_RELEASE_LOCK = Script(RELEASE_LOCK_SCRIPT)  # type: ignore[no-untyped-call]
_ADD_TAG = Script(ADD_TAG_SCRIPT)  # type: ignore[no-untyped-call]
_EXTEND_LOCK = Script(EXTEND_LOCK_SCRIPT)  # type: ignore[no-untyped-call]
_INCR = Script(INCR_SCRIPT)  # type: ignore[no-untyped-call]
_INCR_WINDOW = Script(INCR_WINDOW_SCRIPT)  # type: ignore[no-untyped-call]


class GlideBackend(Backend):
    """Valkey (or Redis) through Valkey GLIDE, the official client with a Rust core:
    `GlideClient` or `GlideClusterClient`, created with `await GlideClient.create(config)`.

    Same behaviour as RedisBackend: SCAN + UNLINK to clear a namespace, distributed locks, tags and
    counters.
    """

    supports_locks = True
    supports_tags = True
    supports_counters = True

    def __init__(self, client: GlideClient | GlideClusterClient, *, scan_count: int = 500) -> None:
        if scan_count < 1:
            raise ValueError("scan_count must be at least 1")
        self.client = client
        self.scan_count = scan_count
        self._is_cluster = isinstance(client, GlideClusterClient)

    async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
        # TTL and GET in one round trip (a non-atomic batch, i.e. a pipeline).
        batch = ClusterBatch(is_atomic=False) if self._is_cluster else Batch(is_atomic=False)
        batch.ttl(key)
        batch.get(key)
        replies = await self.client.exec(batch, raise_on_error=True)
        ttl, value = replies if replies is not None else (0, None)
        if value is None:
            return 0, None
        return int(ttl), value

    async def get(self, key: str) -> bytes | None:
        return await self.client.get(key)

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        expiry = ExpirySet(ExpiryType.SEC, expire) if expire else None
        await self.client.set(key, value, expiry=expiry)

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        if namespace:
            return await self._delete_matching(f"{escape_glob(namespace)}:*")
        if key:
            return await self.client.delete([key])
        return 0

    async def acquire_lock(self, key: str, token: str, ttl: float) -> bool:
        result = await self.client.set(
            key,
            token,
            conditional_set=ConditionalChange.ONLY_IF_DOES_NOT_EXIST,
            expiry=ExpirySet(ExpiryType.MILLSEC, max(1, int(ttl * 1000))),
        )
        return result is not None

    async def release_lock(self, key: str, token: str) -> None:
        await self.client.invoke_script(_RELEASE_LOCK, keys=[key], args=[token])

    async def extend_lock(self, key: str, token: str, ttl: float) -> bool:
        milliseconds = str(max(1, int(ttl * 1000)))
        return bool(
            await self.client.invoke_script(_EXTEND_LOCK, keys=[key], args=[token, milliseconds])
        )

    async def add_tags(self, key: str, tags: Sequence[str], expire: int | None) -> None:
        for tag in tags:
            await self.client.invoke_script(_ADD_TAG, keys=[tag], args=[key, str(expire or 0)])

    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        value = await self.client.invoke_script(
            _INCR, keys=[key], args=[str(amount), str(expire or 0)]
        )
        return int(value)

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
        added, current, previous = await self.client.invoke_script(
            _INCR_WINDOW,
            keys=[key, previous_key],
            args=window_args(amount, limit, weight, expire),
        )
        return bool(added), int(current), int(previous)

    async def invalidate_tags(self, tags: Sequence[str]) -> int:
        return (await self.invalidate_tags_with_keys(tags))[0]

    async def invalidate_tags_with_keys(self, tags: Sequence[str]) -> tuple[int, list[str] | None]:
        deleted, keys = 0, []
        for tag in tags:
            pending = pending_tag_key(tag)
            try:
                await self.client.rename(tag, pending)
            except Exception as exc:
                if not is_missing_key_error(exc):
                    raise
                continue
            members = list(await self.client.smembers(pending))
            if members:
                deleted += await self.client.unlink(list(members))
                keys.extend(key_names(members))
            await self.client.unlink([pending])
        return deleted, keys

    async def _delete_matching(self, pattern: str) -> int:
        deleted = 0
        if isinstance(self.client, GlideClusterClient):
            cursor = ClusterScanCursor()  # type: ignore[no-untyped-call]
            while not cursor.is_finished():
                cursor, keys = await self.client.scan(cursor, match=pattern, count=self.scan_count)  # type: ignore[assignment]
                if keys:
                    deleted += await self.client.unlink(keys)
            return deleted
        position: bytes = b"0"
        while True:
            position, keys = await self.client.scan(position, match=pattern, count=self.scan_count)  # type: ignore[assignment]
            if keys:
                deleted += await self.client.unlink(keys)
            if position == b"0":
                return deleted
