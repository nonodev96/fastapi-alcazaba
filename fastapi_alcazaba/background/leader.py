"""One leader among the processes that share the cache's backend, for work that must run in exactly
one of them all the time (a queue consumer, an outbox relay, a watcher) rather than at given times
(that's FastAPIScheduler).

    leader = FastAPILeader("outbox")

    @leader.run
    async def relay_outbox() -> None:
        while True:
            await relay_pending()
            await asyncio.sleep(1)

    app = FastAPI(lifespan=leader.lifespan)

Every process campaigns for a lock named after the leader (a `fastapi_alcazaba.background.Lock`),
renewed while it's held. The one that takes it leads and runs the functions given to `run`; the
others try again every `ttl / 3` seconds or so. A leader that loses the lock (its renewals
couldn't keep up) cancels its functions and campaigns again. A leader that stops releases the
lock, and another process takes over within `ttl / 3` seconds; one that dies, within `ttl` and a
third. While the backend is down nobody is elected: no leader is better than two.

Leadership is a lock on a cache-like store, so it isn't absolute: a leader that stalls for longer
than `ttl` (a long garbage collection, a network partition) can overlap the next one for a moment.
Check `is_leader` between the steps of long work, and keep that work idempotent.
"""

import asyncio
import inspect
import logging
import math
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, TypeVar

from fastapi_alcazaba.background.locks import Lock
from fastapi_alcazaba.core.errors import BackendUnavailable, CircuitOpen
from fastapi_alcazaba.response_cache.caches import Cache, resolve_cache

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

F = TypeVar("F", bound=Callable[[], Awaitable[Any]])

_random = secrets.SystemRandom()


def _name_of(func: Callable[..., Any]) -> str:
    return f"{func.__module__}.{getattr(func, '__qualname__', func)}"


class FastAPILeader:
    """Elects one process as the leader named `name` and runs functions there while it leads.

    :param name: the leadership's name in the backend; processes with the same name and backend
        compete for it.
    :param ttl: seconds after which the leadership of a process that died is lost; renewed every
        `ttl / 3` seconds while held.
    :param cache: the Cache whose backend holds the lock; FastAPICache's by default, resolved at
        `start()`.
    """

    def __init__(self, name: str, *, ttl: float = 15.0, cache: Cache | None = None) -> None:
        if not name:
            raise ValueError("the leadership needs a name")
        if not (math.isfinite(ttl) and ttl > 0):
            raise ValueError("ttl must be a positive number of seconds")
        self.name = name
        self.ttl = ttl
        self.cache = cache
        self._funcs: list[Callable[[], Awaitable[Any]]] = []
        self._campaign: asyncio.Task[None] | None = None
        self._lock: Lock | None = None
        self._stopping = False
        # How often a leader checks that it still holds the lock: it stops its functions at most
        # this long after the lock lapses.
        self._check = min(ttl / 10, 1.0)

    @property
    def is_leader(self) -> bool:
        """Whether this process leads now."""
        lock = self._lock
        if lock is None:
            return False
        try:
            return lock.held
        except RuntimeError:  # no running loop: a sync endpoint's thread
            return True  # as of the leader's last check, at most a second ago

    def run(self, func: F) -> F:
        """Run the decorated async function (no arguments) whenever this process leads; it's
        cancelled when the leadership is lost or the leader stops, and runs again at the next
        election. It must let the cancellation through. Add them before `start()`."""
        is_async = inspect.iscoroutinefunction(func)  # a bool: mypy would narrow `func` away from F
        if not is_async:
            raise TypeError(f"{_name_of(func)} must be an async function")
        try:
            inspect.signature(func).bind()
        except TypeError:
            raise TypeError(f"{_name_of(func)} must take no arguments") from None
        if self._campaign is not None:
            raise RuntimeError("add functions before the leader starts")
        self._funcs.append(func)
        return func

    async def start(self) -> None:
        """Start campaigning."""
        if self._campaign is not None:
            raise RuntimeError(f"the leader {self.name!r} is already running")
        # Created now so that a backend that can't hold (and renew) locks fails here.
        cache = resolve_cache(self.cache)
        # Its waits aren't recorded: the followers try it over and over.
        lock = Lock(cache, f"leader:{self.name}", ttl=self.ttl, record_wait=False)
        loop = asyncio.get_running_loop()
        self._stopping = False
        self._campaign = loop.create_task(self._campaign_loop(lock), name=f"leader {self.name}")

    async def stop(self) -> None:
        """Stop: the functions are cancelled and the leadership released, for another process to
        take over."""
        campaign, self._campaign = self._campaign, None
        if campaign is None:
            return
        # Told as well as cancelled: a client that swallows the cancellation (fakeredis does on
        # Python 3.11) would otherwise keep the campaign going, and stop() waiting for it.
        self._stopping = True
        campaign.cancel()
        await asyncio.wait({campaign})

    @asynccontextmanager
    async def lifespan(self, app: Any = None) -> AsyncIterator[None]:
        """For `FastAPI(lifespan=leader.lifespan)`: campaigns while the app runs."""
        await self.start()
        try:
            yield
        finally:
            await self.stop()

    async def __aenter__(self) -> "FastAPILeader":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def _campaign_loop(self, lock: Lock) -> None:
        while not self._stopping:
            try:
                elected = await lock.acquire()
            except BackendUnavailable as exc:
                if isinstance(exc, CircuitOpen):
                    logger.debug("No election of %r: %s", self.name, exc)
                else:
                    logger.warning("No election of %r: the backend failed", self.name, exc_info=exc)
                elected = False
            if elected:
                await self._lead(lock)
            if not self._stopping:
                # Jittered, so that the followers don't all ask at once.
                await asyncio.sleep(self.ttl / 3 * _random.uniform(0.5, 1))

    async def _lead(self, lock: Lock) -> None:
        self._lock = lock
        logger.info("This process leads %r", self.name)
        metrics, event = lock.cache.metrics, "resigned"
        metrics.count("leader.transitions", leader=self.name, event="elected")
        loop = asyncio.get_running_loop()
        tasks = [
            loop.create_task(self._guarded(func), name=f"leader {self.name}: {_name_of(func)}")
            for func in self._funcs
        ]
        try:
            while lock.held and not self._stopping:
                await asyncio.sleep(self._check)
            if not self._stopping:
                logger.warning("Lost the leadership of %r: its functions are cancelled", self.name)
                event = "lost"
        finally:
            metrics.count("leader.transitions", leader=self.name, event=event)
            self._lock = None
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks)
            await lock.release()

    async def _guarded(self, func: Callable[[], Awaitable[Any]]) -> None:
        try:
            await func()
        except Exception:
            logger.exception("%s failed while leading %r", _name_of(func), self.name)
