"""Concurrency limits on the cache's backend: how many requests may run at the same time, across
every process, for a route (or a group of routes), optionally per client.

    ocr = FastAPIConcurrencyLimiter(4, name="ocr", wait=2)

    @app.post("/invoices/scan", dependencies=[Depends(ocr)])
    async def scan(file: UploadFile) -> Invoice: ...

    async with ocr.slot():  # outside requests: a job, a queue consumer
        await scan_pending()

FastAPIRateLimiter counts requests per window; this counts the ones running now, which is what
protects endpoints whose cost is how long they run (AI, OCR, PDF rendering). When all `limit`
slots are taken, a request waits up to `wait` seconds for one, then gets a 503 (a 429 when the
limit is per client, with `identifier`) with `Retry-After`.

The semaphore is `limit` locks in the backend (`fastapi_alcazaba.locks`): a request takes a free
one, trying them in random order so that the processes don't all queue on the first, renews it
while it runs and releases it when it's done; the slots of a process that dies expire after `ttl`
seconds. Each busy slot tried costs a round trip, so `limit` is capped at MAX_LIMIT. Waiters poll
with backoff: there's no queue, and no order among them.

When the slot is released: since FastAPI 0.118 the code after a dependency's `yield` runs once
the response has been sent, so the slot is held until the last byte of a StreamingResponse;
`Depends(limiter, scope="function")` (FastAPI 0.121) releases it when the endpoint returns, before
the body is sent. FastAPI 0.115 to 0.117 always release it before the response is sent: a
streamed body runs outside the limit there (take a `slot()` inside the stream instead). In a
WebSocket route the slot is held while the connection is open.
"""

import asyncio
import hashlib
import json
import logging
import math
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from inspect import isawaitable

from fastapi import HTTPException, WebSocketException
from starlette.requests import HTTPConnection
from starlette.status import (
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_503_SERVICE_UNAVAILABLE,
    WS_1008_POLICY_VIOLATION,
    WS_1013_TRY_AGAIN_LATER,
)

from fastapi_alcazaba.cache import Cache, get_cache
from fastapi_alcazaba.errors import BackendUnavailable, CircuitOpen
from fastapi_alcazaba.limiter import Identifier, _route_scope
from fastapi_alcazaba.locks import Lock
from fastapi_alcazaba.tokens import _resolve

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: The largest `limit`: taking a slot when most are busy costs a round trip per busy slot.
MAX_LIMIT = 100

_random = secrets.SystemRandom()
_FIRST_PAUSE = 0.025
_LONGEST_PAUSE = 0.4


class ConcurrencyLimitExceeded(HTTPException):
    """Every slot stayed taken for as long as the limiter waits. FastAPI answers it with a 503 (a
    429 when the limit is per client) and `Retry-After`."""

    def __init__(self, status_code: int, retry_after: int) -> None:
        detail = (
            "Too Many Requests"
            if status_code == HTTP_429_TOO_MANY_REQUESTS
            else "Service Unavailable"
        )
        super().__init__(status_code, detail, headers={"Retry-After": str(retry_after)})
        self.retry_after = retry_after


@dataclass(slots=True)
class _Slot:
    base: str
    lock: Lock | None  # None: counted in this process only, while the backend failed
    index: int = -1


class FastAPIConcurrencyLimiter:
    """FastAPI dependency (with `yield`) that lets at most `limit` requests run at the same time;
    the slot is released when the request is done (see the module's docstring for streaming).

        @app.post("/summaries", dependencies=[Depends(FastAPIConcurrencyLimiter(8, wait=5))])

    The slots live in the cache's backend (the `cache` given, else the one `get_cache` returns),
    so every process shares them. Their keys are SHA-256 digests: identities never reach the
    backend in clear.

    :param limit: requests that may run at the same time, 1 to MAX_LIMIT.
    :param name: routes whose limiters have this name and the same `limit` share the slots; by
        default each route (its methods and path template) has its own. Needed by `slot()`
        outside requests.
    :param identifier: who the client is (e.g. `client_ip`, or a user id): each one gets `limit`
        slots of its own, and is refused with a 429 instead of a 503. None: every client shares
        them.
    :param wait: seconds to wait for a slot when they're all taken; 0 tries once.
    :param ttl: seconds after which the slot of a process that died is freed; renewed every
        `ttl / 3` seconds while the request runs.
    :param cache: the Cache whose backend keeps the slots.
    :param fail_open: when the backend fails, count the slots in each process instead (so at most
        `limit` per process run; the default), or refuse requests with a 503.
    :param retry_after: the `Retry-After` of refused requests, in seconds.
    """

    def __init__(
        self,
        limit: int,
        *,
        name: str | None = None,
        identifier: Identifier | None = None,
        wait: float = 0.0,
        ttl: float = 60.0,
        cache: Cache | None = None,
        fail_open: bool = True,
        retry_after: int = 1,
    ) -> None:
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        if not (math.isfinite(wait) and wait >= 0):
            raise ValueError("wait must be a finite number of seconds, 0 or more")
        if not (math.isfinite(ttl) and ttl > 0):
            raise ValueError("ttl must be a positive number of seconds")
        if retry_after < 1:
            raise ValueError("retry_after must be at least 1 second")
        if cache is not None:
            _check_locks(cache)
        self.limit = limit
        self.name = name
        self.identifier = identifier
        self.wait = wait
        self.ttl = ttl
        self.cache = cache
        self.fail_open = fail_open
        self.retry_after = retry_after
        self._status = (
            HTTP_429_TOO_MANY_REQUESTS if identifier is not None else HTTP_503_SERVICE_UNAVAILABLE
        )
        # Bounded by the requests running: entries go when their last slot is released.
        self._held: dict[str, set[int]] = {}  # slots this process holds in the backend
        self._local: dict[str, int] = {}  # slots taken while the backend failed

    async def __call__(self, connection: HTTPConnection) -> AsyncIterator[None]:
        websocket = connection.scope["type"] == "websocket"
        try:
            slot = await self._take(connection, "")
        except BackendUnavailable as exc:
            if websocket:
                raise WebSocketException(WS_1013_TRY_AGAIN_LATER) from exc
            headers = {"Retry-After": str(self.retry_after)}
            raise HTTPException(HTTP_503_SERVICE_UNAVAILABLE, headers=headers) from exc
        if slot is None:
            if websocket:
                per_client = self.identifier is not None
                code = WS_1008_POLICY_VIOLATION if per_client else WS_1013_TRY_AGAIN_LATER
                raise WebSocketException(code)
            raise ConcurrencyLimitExceeded(self._status, self.retry_after)
        try:
            yield
        finally:
            await self._give_back(slot)

    @asynccontextmanager
    async def slot(
        self,
        key: str = "",
        *,
        connection: HTTPConnection | None = None,
    ) -> AsyncIterator[None]:
        """Hold a slot around a block, outside a dependency:

            async with limiter.slot(key=tenant_id):
                await render(report)

        `key` gives each value its own `limit` slots. With a `connection`, the route and the
        `identifier` apply as in the dependency; without one, the limiter needs a `name`. Raises
        ConcurrencyLimitExceeded when no slot frees within `wait` seconds, and BackendUnavailable
        when the backend fails and `fail_open` is False."""
        taken = await self._take(connection, key)
        if taken is None:
            raise ConcurrencyLimitExceeded(self._status, self.retry_after)
        try:
            yield
        finally:
            await self._give_back(taken)

    async def _take(self, connection: HTTPConnection | None, key: str) -> _Slot | None:
        if self.name is not None:
            scope = self.name
        elif connection is not None:
            scope = _route_scope(connection)
        else:
            raise ValueError("outside requests, slot() needs a limiter with a name")
        identity = ""
        if connection is None:
            cache = _resolve(self.cache)
        else:
            cache = self.cache if self.cache is not None else get_cache(connection)
            if self.identifier is not None:
                found = self.identifier(connection)
                if isawaitable(found):
                    found = await found
                identity = str(found)
        _check_locks(cache)
        parts = [scope, self.limit, identity, key]
        base = hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32]
        return await self._acquire(cache, base, scope)

    async def _acquire(self, cache: Cache, base: str, scope: str) -> _Slot | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.wait
        pause = _FIRST_PAUSE
        while True:
            try:
                slot, outcome = await self._try_backend(cache, base), "acquired"
            except BackendUnavailable as exc:
                if not self.fail_open:
                    logger.warning("Concurrency limit of %s refused: the backend failed", scope)
                    cache.metrics.count("concurrency", outcome="unavailable", limiter=scope)
                    raise
                if isinstance(exc, CircuitOpen):
                    logger.debug("Concurrency limit of %s counted in this process: %s", scope, exc)
                else:
                    logger.warning(
                        "Concurrency limit of %s counted in this process: the backend failed",
                        scope,
                        exc_info=exc,
                    )
                slot, outcome = self._try_local(base), "local"
            if slot is not None:
                cache.metrics.count("concurrency", outcome=outcome, limiter=scope)
                return slot
            left = deadline - loop.time()
            if left <= 0:
                cache.metrics.count("concurrency", outcome="refused", limiter=scope)
                return None
            # Jittered, so that the waiters of every process don't poll in step.
            await asyncio.sleep(min(pause * _random.uniform(0.5, 1), left))
            pause = min(pause * 2, _LONGEST_PAUSE)

    async def _try_backend(self, cache: Cache, base: str) -> _Slot | None:
        mine = self._held.get(base, set())
        free = [index for index in range(self.limit) if index not in mine]
        _random.shuffle(free)
        for index in free:
            lock = Lock(cache, f"concurrency:{base}:{index}", ttl=self.ttl, record_wait=False)
            if await lock.acquire():
                self._held.setdefault(base, set()).add(index)
                return _Slot(base, lock, index)
        return None

    def _try_local(self, base: str) -> _Slot | None:
        """A slot counted in this process only, the ones it holds in the backend included."""
        if len(self._held.get(base, ())) + self._local.get(base, 0) >= self.limit:
            return None
        self._local[base] = self._local.get(base, 0) + 1
        return _Slot(base, None)

    async def _give_back(self, slot: _Slot) -> None:
        if slot.lock is None:
            left = self._local[slot.base] - 1
            if left:
                self._local[slot.base] = left
            else:
                del self._local[slot.base]
            return
        try:
            await slot.lock.release()
        finally:
            held = self._held[slot.base]
            held.discard(slot.index)
            if not held:
                del self._held[slot.base]


def _check_locks(cache: Cache) -> None:
    if not cache.backend.supports_locks:
        raise NotImplementedError(
            f"{type(cache.backend).__name__} can't hold locks, which the concurrency limiter needs"
        )
