import abc
import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import Response

_Func = Callable[..., Any]

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def is_state_key(key: str) -> bool:
    """Whether `key` holds state rather than a cached response (see `Cache.state_key`): its first
    `!` comes before its first `:`."""
    mark = key.find("!")
    return mark != -1 and mark < key.find(":")


class KeyBuilder(Protocol):
    def __call__(
        self,
        function: _Func,
        namespace: str = ...,
        /,
        *,
        request: Request | None = ...,
        response: Response | None = ...,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Awaitable[str] | str: ...


def _unsupported(backend: "Backend", what: str) -> NotImplementedError:
    """The error of what a backend can't do: its `supports_*` says so, and it doesn't override the
    method (not an abstract one: every backend needn't have every capability)."""
    return NotImplementedError(f"{type(backend).__name__} can't {what}")


class Backend(abc.ABC):
    #: Can hold a lock shared by every process (see `distributed_lock`).
    supports_locks: bool = False
    #: Can delete entries by tag (see `tags` and `FastAPICache.invalidate_tags`).
    supports_tags: bool = False
    #: Can keep counters (see `incr` and `incr_window`), which the rate limiter needs.
    supports_counters: bool = False

    @abc.abstractmethod
    async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
        """(seconds left, value); -1 seconds when the entry doesn't expire or the backend can't
        tell, and (0, None) when there is no entry."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get(self, key: str) -> bytes | None:
        raise NotImplementedError

    @abc.abstractmethod
    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        """Delete every entry of `namespace` (keys starting with "<namespace>:"), or else the
        entry `key`; returns how many were deleted."""
        raise NotImplementedError

    async def acquire_lock(self, key: str, token: str, ttl: float) -> bool:
        """Take the lock `key` for `ttl` seconds unless someone holds it; True if taken."""
        raise _unsupported(self, "hold locks")

    async def release_lock(self, key: str, token: str) -> None:
        """Release the lock `key` if it's still held with `token` (it may have expired)."""
        raise _unsupported(self, "hold locks")

    async def extend_lock(self, key: str, token: str, ttl: float) -> bool:
        """Make the lock `key` expire `ttl` seconds from now if it's still held with `token`;
        False when it isn't (it expired, maybe taken by someone else)."""
        raise _unsupported(self, "hold locks")

    async def add_tags(self, key: str, tags: Sequence[str], expire: int | None) -> None:
        """Record that the entry `key` belongs to each of `tags` (already namespaced keys)."""
        raise _unsupported(self, "keep tags")

    async def invalidate_tags(self, tags: Sequence[str]) -> int:
        """Delete every entry recorded under `tags`; returns how many were deleted."""
        raise _unsupported(self, "keep tags")

    async def invalidate_tags_with_keys(self, tags: Sequence[str]) -> tuple[int, list[str] | None]:
        """`invalidate_tags`, also returning the keys of the entries recorded under `tags`, or
        None when the backend can't tell (TieredBackend tells the other processes to drop their
        copies of them)."""
        return await self.invalidate_tags(tags), None

    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        """Add `amount` to the counter `key` atomically and return its new value. A missing counter
        starts at 0 and, with `expire`, is deleted that many seconds after it's created."""
        raise _unsupported(self, "keep counters")

    async def get_counter(self, key: str) -> int:
        """The value of the counter `key`; 0 when there is none."""
        value = await self.get(key)
        return int(value) if value else 0

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
        """The rate limiter's sliding window: add `amount` to the counter `key` only if
        `previous * weight + current + amount <= limit`, where `current` is that counter and
        `previous` the counter `previous_key`. Returns whether it was added and both counts
        (`current` with the addition, if any).

        Backends that can, check and add atomically. This default adds and then takes the amount
        back when it didn't fit, so concurrent requests may briefly see it."""
        current, previous = await asyncio.gather(
            self.incr(key, amount, expire), self.get_counter(previous_key)
        )
        if previous * weight + current <= limit:
            return True, current, previous
        try:
            current = await self.incr(key, -amount, expire)
        except Exception:
            # Still refused; the counter keeps the amount until it expires.
            logger.warning("Couldn't take back %s from the counter %r", amount, key, exc_info=True)
        return False, current, previous
