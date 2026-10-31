import math
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi_alcazaba.backends.base import Backend, is_state_key
from fastapi_alcazaba.core.errors import BackendUnavailable


@dataclass(slots=True)
class Value:
    data: bytes
    expires_at: float | None  # time.monotonic(); None: no expiry


class InMemoryBackend(Backend):
    """Cache in the memory of the process: for development, tests and single-process apps.

    Each instance has its own store, bounded to `max_size` entries (the least recently used are
    evicted): a store that never evicted would grow with every new query string until the
    process ran out of memory.

    State (rate-limit counters, revoked tokens, one-time codes, sessions, idempotency records:
    `Cache.state_key`) is never evicted while it lives: forgetting a revocation would accept the
    token again. Each kind of state has a store of its own, bounded to `max_size` live entries;
    a write to a full one raises BackendUnavailable, like Redis with `noeviction`, and each tool
    fails closed. So a flood of idempotency keys fills the idempotency records, never the
    revocations or the sessions.
    """

    supports_locks = True  # within the process
    supports_tags = True
    supports_counters = True

    def __init__(self, max_size: int | None = 10_000) -> None:
        if max_size is not None and max_size < 1:
            raise ValueError("max_size must be at least 1 (or None for no limit)")
        self.max_size = max_size
        self._store: OrderedDict[str, Value] = OrderedDict()
        self._state: dict[str, OrderedDict[str, Value]] = {}  # kind -> its state
        self._swept_at: dict[str, float] = {}  # kind -> monotonic time of its last sweep
        self._tags: dict[str, set[str]] = {}
        self._tags_of: dict[str, set[str]] = {}  # entry key -> its tags
        # Apart from the entries, so that evicting the least recently used never frees a lock.
        self._locks: dict[str, tuple[str, float]] = {}  # key -> (token, expiry in monotonic time)

    def _place(self, key: str) -> OrderedDict[str, Value]:
        if not is_state_key(key):
            return self._store
        return self._state.setdefault(_kind(key), OrderedDict())

    def _delete(self, key: str) -> bool:
        if self._place(key).pop(key, None) is None:
            return False
        for tag in self._tags_of.pop(key, ()):
            members = self._tags.get(tag)
            if members is not None:
                members.discard(key)
                if not members:
                    del self._tags[tag]
        return True

    def _get(self, key: str) -> Value | None:
        place = self._place(key)
        value = place.get(key)
        if value is None:
            return None
        if value.expires_at is not None and value.expires_at <= time.monotonic():
            self._delete(key)
            return None
        place.move_to_end(key)
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
        place = self._place(key)
        place[key] = Value(value, expires_at)
        place.move_to_end(key)
        if self.max_size is None or len(place) <= self.max_size:
            return
        if place is self._store:
            while len(place) > self.max_size:
                self._delete(next(iter(place)))
            return
        self._sweep(_kind(key), place)
        if len(place) > self.max_size:
            self._delete(key)
            raise BackendUnavailable(
                f"the in-memory state of {_kind(key)!r} is full: {self.max_size} live entries"
            )

    def _sweep(self, kind: str, place: OrderedDict[str, Value]) -> None:
        """Drop the expired state of `kind`, at most once a second."""
        now = time.monotonic()
        if now - self._swept_at.get(kind, 0.0) < 1:
            return
        self._swept_at[kind] = now
        expired = [k for k, v in place.items() if v.expires_at and v.expires_at <= now]
        for key in expired:
            self._delete(key)

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        if namespace:
            prefix = f"{namespace}:"
            state = (key for place in self._state.values() for key in place)
            matching = [k for k in (*self._store, *state) if k.startswith(prefix)]
            for k in matching:
                self._delete(k)
            return len(matching)
        if key:
            return int(self._delete(key))
        return 0

    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        value = self._get(key)
        if value is None:
            await self.set(key, str(amount).encode(), expire)
            return amount
        count = int(value.data) + amount
        value.data = str(count).encode()
        return count

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
        """Atomic: nothing awaits between the check and the addition."""
        current, previous = (int(v.data) if (v := self._get(k)) else 0 for k in (key, previous_key))
        if previous * weight + current + amount > limit:
            return False, current, previous
        return True, await self.incr(key, amount, expire), previous

    def _lock_holder(self, key: str) -> str | None:
        held = self._locks.get(key)
        if held is None:
            return None
        if held[1] <= time.monotonic():
            del self._locks[key]
            return None
        return held[0]

    async def acquire_lock(self, key: str, token: str, ttl: float) -> bool:
        if self._lock_holder(key) is not None:
            return False
        if len(self._locks) >= (self.max_size or 10_000):
            # Locks whose holder never released them: drop the expired ones.
            now = time.monotonic()
            for expired in [k for k, (_, expiry) in self._locks.items() if expiry <= now]:
                del self._locks[expired]
        self._locks[key] = (token, time.monotonic() + ttl)
        return True

    async def release_lock(self, key: str, token: str) -> None:
        if self._lock_holder(key) == token:
            del self._locks[key]

    async def extend_lock(self, key: str, token: str, ttl: float) -> bool:
        if self._lock_holder(key) != token:
            return False
        self._locks[key] = (token, time.monotonic() + ttl)
        return True

    async def add_tags(self, key: str, tags: Sequence[str], expire: int | None) -> None:
        if key not in self._place(key):
            return
        for tag in tags:
            self._tags.setdefault(tag, set()).add(key)
        self._tags_of.setdefault(key, set()).update(tags)

    async def invalidate_tags(self, tags: Sequence[str]) -> int:
        return (await self.invalidate_tags_with_keys(tags))[0]

    async def invalidate_tags_with_keys(self, tags: Sequence[str]) -> tuple[int, list[str] | None]:
        keys = set().union(*(self._tags.get(tag, set()) for tag in tags))
        deleted = [key for key in keys if self._delete(key)]
        return len(deleted), deleted


def _kind(key: str) -> str:
    """The kind of a state key, `{prefix}!{kind}:...`."""
    return key[key.find("!") + 1 : key.find(":")]
