"""Locks shared by every process, on the cache's backend: for work that only one instance must do
at a time, such as a job run at startup by every container or a payment being processed.

    async with cache.lock("nightly-report", ttl=60):
        await build_report()

A lock expires after `ttl` seconds, so a holder that dies doesn't keep it forever, and while it's
held a background task renews it every `ttl / 3` seconds. Only the holder can release or renew
it: each acquisition has its own random token.

No lock on a cache-like store is absolute: a holder that stalls for longer than `ttl` (a long
garbage collection, a network partition) loses it while it still thinks it holds it. `held` says
whether the renewals are keeping up; check it between the steps of long jobs.
"""

import asyncio
import hashlib
import logging
import secrets
from types import TracebackType
from typing import TYPE_CHECKING

from fastapi_alcazaba.backends.tiered import TieredBackend
from fastapi_alcazaba.errors import BackendUnavailable
from fastapi_alcazaba.types import Backend

if TYPE_CHECKING:
    from fastapi_alcazaba.cache import Cache

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def _renews(backend: Backend) -> bool:
    """Whether the backend can extend a lock (backends written for 0.1 can't)."""
    if isinstance(backend, TieredBackend):
        backend = backend.remote
    return type(backend).extend_lock is not Backend.extend_lock


class LockNotAcquired(Exception):
    """Someone else held the lock for longer than the time to wait for it."""


class Lock:
    """A lock named `name` in the cache's backend (`Cache.lock` creates them).

    :param ttl: seconds after which the lock expires if its holder neither renews nor releases it.
    :param wait: seconds to wait for the lock when someone else holds it; 0 tries once, None
        waits as long as it takes.
    :param renew: keep renewing it while it's held.
    """

    def __init__(
        self,
        cache: "Cache",
        name: str,
        *,
        ttl: float = 30.0,
        wait: float | None = 0.0,
        renew: bool = True,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        if wait is not None and wait < 0:
            raise ValueError("wait can't be negative")
        if not cache.backend.supports_locks:
            raise NotImplementedError(f"{type(cache.backend).__name__} can't hold locks")
        if renew and not _renews(cache.backend):
            raise NotImplementedError(
                f"{type(cache.backend).__name__} can't renew locks (it has no extend_lock): "
                "pass renew=False, with a ttl longer than the work"
            )
        self.cache = cache
        self.name = name
        self.ttl = ttl
        self.wait = wait
        self.renew = renew
        digest = hashlib.sha256(name.encode()).hexdigest()[:32]
        self.key = cache.state_key("lock", f"{{{digest}}}")
        self._token: str | None = None
        self._expires_at = 0.0  # loop time
        self._renewal: asyncio.Task[None] | None = None

    @property
    def held(self) -> bool:
        """Acquired, not released, and not expired since the last renewal."""
        loop = asyncio.get_running_loop()
        return self._token is not None and loop.time() < self._expires_at

    async def acquire(self) -> bool:
        """Take the lock, waiting up to `wait` seconds; False if someone else still holds it.
        Raises BackendUnavailable when the backend fails: without it nobody can tell who holds
        the lock."""
        if self._token is not None:
            raise RuntimeError(f"the lock {self.name!r} is already held by this Lock")
        loop = asyncio.get_running_loop()
        deadline = None if self.wait is None else loop.time() + self.wait
        token, delay = secrets.token_hex(16), 0.025
        while True:
            started = loop.time()
            acquired = await self.cache.guard.run(
                "lock", lambda: self.cache.backend.acquire_lock(self.key, token, self.ttl)
            )
            if acquired:
                break
            if deadline is not None and loop.time() >= deadline:
                return False
            pause = delay if deadline is None else min(delay, deadline - loop.time())
            await asyncio.sleep(max(pause, 0))
            delay = min(delay * 2, 0.4)
        self._token = token
        self._expires_at = started + self.ttl
        if self.renew:
            self._renewal = asyncio.create_task(self._keep(token))
        return True

    async def release(self) -> None:
        """Release it, if this Lock holds it. A backend failure is only logged: the lock expires
        after `ttl` anyway."""
        token, self._token = self._token, None
        renewal, self._renewal = self._renewal, None
        try:
            if renewal is not None:
                renewal.cancel()
                await asyncio.wait({renewal})  # a cancellation of the caller still goes through
        finally:
            if token is not None:
                await self._unlock(token)

    async def _unlock(self, token: str) -> None:
        try:
            await self.cache.guard.run(
                "unlock", lambda: self.cache.backend.release_lock(self.key, token)
            )
        except BackendUnavailable:
            logger.warning("Couldn't release the lock %r: it expires by itself", self.name)

    async def _keep(self, token: str) -> None:
        """Renew the lock every ttl / 3 until it's released or lost."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.ttl / 3)
            started = loop.time()
            try:
                renewed = await self.cache.guard.run(
                    "extend_lock",
                    lambda: self.cache.backend.extend_lock(self.key, token, self.ttl),
                )
            except BackendUnavailable:
                if loop.time() < self._expires_at:
                    continue  # it may come back before the lock expires
                renewed = False
            if not renewed:
                logger.warning("Lost the lock %r: it expired before it could be renewed", self.name)
                self._expires_at = 0.0
                return
            self._expires_at = started + self.ttl

    async def __aenter__(self) -> "Lock":
        if not await self.acquire():
            raise LockNotAcquired(f"the lock {self.name!r} is held by someone else")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.release()
