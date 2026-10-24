import abc
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import Response

_Func = Callable[..., Any]


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


class Backend(abc.ABC):
    #: Can hold a lock shared by every process (see `distributed_lock`).
    supports_locks: bool = False
    #: Can delete entries by tag (see `tags` and `FastAPICache.invalidate_tags`).
    supports_tags: bool = False

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
        raise NotImplementedError

    async def release_lock(self, key: str, token: str) -> None:
        """Release the lock `key` if it's still held with `token` (it may have expired)."""
        raise NotImplementedError

    async def add_tags(self, key: str, tags: Sequence[str], expire: int | None) -> None:
        """Record that the entry `key` belongs to each of `tags` (already namespaced keys)."""
        raise NotImplementedError

    async def invalidate_tags(self, tags: Sequence[str]) -> int:
        """Delete every entry recorded under `tags`; returns how many were deleted."""
        raise NotImplementedError
