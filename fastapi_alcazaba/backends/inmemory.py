import math
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi_alcazaba.types import Backend


@dataclass(slots=True)
class Value:
    data: bytes
    expires_at: float | None  # time.monotonic(); None: no expiry


class InMemoryBackend(Backend):
    """Cache in the memory of the process: for development, tests and single-process apps.

    Each instance has its own store, bounded to `max_size` entries (the least recently used are
    evicted). fastapi-cache2 shared one dict between all instances and never evicted, so varying
    a query string grew it until the process ran out of memory.
    """

    supports_tags = True

    def __init__(self, max_size: int | None = 10_000) -> None:
        if max_size is not None and max_size < 1:
            raise ValueError("max_size must be at least 1 (or None for no limit)")
        self.max_size = max_size
        self._store: OrderedDict[str, Value] = OrderedDict()
        self._tags: dict[str, set[str]] = {}
        self._tags_of: dict[str, set[str]] = {}  # entry key -> its tags

    def _delete(self, key: str) -> bool:
        if self._store.pop(key, None) is None:
            return False
        for tag in self._tags_of.pop(key, ()):
            members = self._tags.get(tag)
            if members is not None:
                members.discard(key)
                if not members:
                    del self._tags[tag]
        return True

    def _get(self, key: str) -> Value | None:
        value = self._store.get(key)
        if value is None:
            return None
        if value.expires_at is not None and value.expires_at <= time.monotonic():
            self._delete(key)
            return None
        self._store.move_to_end(key)
        return value

    async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
        value = self._get(key)
        if value is None:
            return 0, None
        if value.expires_at is None:
            return -1, value.data
        return max(1, math.ceil(value.expires_at - time.monotonic())), value.data

    async def get(self, key: str) -> bytes | None:
        value = self._get(key)
        return None if value is None else value.data

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        expires_at = time.monotonic() + expire if expire else None
        self._store[key] = Value(value, expires_at)
        self._store.move_to_end(key)
        if self.max_size is not None:
            while len(self._store) > self.max_size:
                self._delete(next(iter(self._store)))

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        if namespace:
            prefix = f"{namespace}:"
            matching = [k for k in self._store if k.startswith(prefix)]
            for k in matching:
                self._delete(k)
            return len(matching)
        if key:
            return int(self._delete(key))
        return 0

    async def add_tags(self, key: str, tags: Sequence[str], expire: int | None) -> None:
        if key not in self._store:
            return
        for tag in tags:
            self._tags.setdefault(tag, set()).add(key)
        self._tags_of.setdefault(key, set()).update(tags)

    async def invalidate_tags(self, tags: Sequence[str]) -> int:
        keys = set().union(*(self._tags.get(tag, set()) for tag in tags))
        return sum(self._delete(key) for key in keys)
