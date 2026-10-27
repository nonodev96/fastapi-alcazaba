"""Maintenance mode shared by every process: while it's on, MaintenanceMiddleware answers 503 with
`Retry-After` to every request but the allowed ones (health checks, an admin path, the team).

    maintenance = FastAPIMaintenance()
    app.add_middleware(MaintenanceMiddleware, maintenance=maintenance)

    await maintenance.enable("Back at 10:30", retry_after=900)  # from an admin route, a script...
    await maintenance.disable()

The state lives in the cache's backend, so turning it on in one process turns it on in all of
them. Requests don't ask the backend each: every process keeps a copy, refreshed at most every
`refresh` seconds, so a change reaches every process within about that.

When the backend fails, each process keeps the last state it read, and serves normally if it
never read one: maintenance often means work on the backend itself, and its failure mustn't
reopen an app that was closed on purpose, nor close one that was open.
"""

import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

from starlette.requests import HTTPConnection
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE, WS_1013_TRY_AGAIN_LATER
from starlette.types import ASGIApp, Receive, Scope, Send

from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.asgi import respond, route_path
from fastapi_alcazaba.core.errors import BackendUnavailable, CircuitOpen
from fastapi_alcazaba.response_cache.caches import Cache, cache_metrics, resolve_cache

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: The longest message: it goes in every refused response.
MAX_MESSAGE_LENGTH = 1_000

#: Decides whether a request goes through during maintenance: receives it (or the WebSocket),
#: returns a bool. Use the address the server reports (`client_ip`), not `X-Forwarded-For`.
Allow = Callable[[HTTPConnection], Awaitable[bool] | bool]


@dataclass(frozen=True, slots=True)
class MaintenanceStatus:
    """Whether maintenance is on, with the message and `Retry-After` of the refused requests and
    since when (Unix time)."""

    enabled: bool
    message: str | None = None
    retry_after: int | None = None
    since: float | None = None


_OFF = MaintenanceStatus(enabled=False)


def _check(message: str | None, retry_after: int | None) -> None:
    if message is not None and len(message) > MAX_MESSAGE_LENGTH:
        raise ValueError(f"the message can't be longer than {MAX_MESSAGE_LENGTH} characters")
    if retry_after is not None and (isinstance(retry_after, bool) or retry_after < 1):
        raise ValueError("retry_after must be at least 1 second")


def _parse(payload: bytes) -> MaintenanceStatus | None:
    """The state stored by `enable`; None when it isn't one."""
    try:
        data: Any = json.loads(payload)
        message, retry_after, since = data["message"], data["retry_after"], data["since"]
        if not (message is None or isinstance(message, str)):
            return None
        if not (retry_after is None or type(retry_after) is int):
            return None
        if type(since) not in (int, float):
            return None
        _check(message, retry_after)
    except (TypeError, ValueError, KeyError):
        return None
    return MaintenanceStatus(True, message, retry_after, float(since))


class FastAPIMaintenance:
    """The maintenance switch, in the cache's backend.

    :param cache: the Cache whose backend keeps the state; FastAPICache's by default, resolved at
        each call.
    :param refresh: seconds each process keeps its copy of the state before asking again.
    :param namespace: part of the key, after the cache's prefix (see `Cache.state_key`).
    """

    def __init__(
        self,
        cache: Cache | None = None,
        *,
        refresh: float = 1.0,
        namespace: str = "maintenance",
    ) -> None:
        if not (math.isfinite(refresh) and refresh > 0):
            raise ValueError("refresh must be a positive number of seconds")
        self.cache = cache
        self.refresh = refresh
        self.namespace = namespace
        self._state = _OFF
        self._checked_at = -math.inf  # monotonic
        # Moves on with every change made here: a read that started before one is stale.
        self._epoch = 0
        self._source: Cache | None = None  # the cache the copy comes from

    def _key(self, cache: Cache) -> str:
        return cache.state_key(self.namespace, "state")

    def forget(self) -> None:
        """Drop this process's copy of the state: the next request reads it from the backend."""
        self._state, self._checked_at = _OFF, -math.inf
        self._epoch += 1

    def _cache(self) -> Cache:
        """The cache; a copy read from another one (the app's set up again, as each test's
        lifespan does) is dropped."""
        cache = resolve_cache(self.cache)
        if cache is not self._source:
            self._source = cache
            self.forget()
        return cache

    async def enable(
        self,
        message: str | None = None,
        retry_after: int | None = None,
    ) -> MaintenanceStatus:
        """Turn maintenance on in every process (within `refresh` seconds), with the `message`
        (the `detail` of the 503s) and `retry_after` seconds to send. Raises BackendUnavailable
        when the backend fails: nothing changes then."""
        _check(message, retry_after)
        cache = self._cache()
        state = MaintenanceStatus(True, message, retry_after, time.time())
        payload = json.dumps(
            {"message": message, "retry_after": retry_after, "since": state.since}
        ).encode()
        key = self._key(cache)
        protector = cache.options.protector
        value = protector.seal(key, payload) if protector is not None else payload
        backend = shared_backend(cache.backend)
        await cache.guard.run("set", lambda: backend.set(key, value))
        self._remember(state)
        logger.warning("Maintenance mode on")
        return state

    async def disable(self) -> None:
        """Turn maintenance off in every process (within `refresh` seconds). Raises
        BackendUnavailable when the backend fails."""
        cache = self._cache()
        key, backend = self._key(cache), shared_backend(cache.backend)
        await cache.guard.run("delete", lambda: backend.clear(key=key))
        self._remember(_OFF)
        logger.warning("Maintenance mode off")

    async def status(self) -> MaintenanceStatus:
        """The state in the backend now; this process's copy is updated with it. Raises
        BackendUnavailable when the backend fails."""
        return await self._status(self._cache())

    async def _status(self, cache: Cache) -> MaintenanceStatus:
        epoch = self._epoch
        state = await self._read(cache)
        if epoch == self._epoch:
            self._state, self._checked_at = state, time.monotonic()
        return state

    async def current(self) -> MaintenanceStatus:
        """This process's copy of the state, refreshed when it's older than `refresh` seconds;
        what the middleware checks on every request. Never raises: when the backend fails, the
        last state read (off if none)."""
        cache, now = self._cache(), time.monotonic()
        if now - self._checked_at < self.refresh:
            return self._state
        self._checked_at = now  # the other requests use the copy while this one asks
        try:
            return await self._status(cache)
        except BackendUnavailable as exc:
            if isinstance(exc, CircuitOpen):
                logger.debug("Maintenance state not refreshed: %s", exc)
            else:
                logger.warning("Maintenance state not refreshed: the backend failed", exc_info=exc)
            return self._state

    def _remember(self, state: MaintenanceStatus) -> None:
        self._state, self._checked_at = state, time.monotonic()
        self._epoch += 1

    async def _read(self, cache: Cache) -> MaintenanceStatus:
        key, backend = self._key(cache), shared_backend(cache.backend)
        data = await cache.guard.run("get", lambda: backend.get(key))
        if data is None:
            return _OFF
        protector = cache.options.protector
        if protector is not None and (data := protector.open(key, data)) is None:
            logger.warning("Ignored a maintenance state that this app didn't write")
            return _OFF
        state = _parse(data)
        if state is None:
            logger.warning("Ignored a malformed maintenance state")
            return _OFF
        return state


class MaintenanceMiddleware:
    """While `maintenance` is on, answers every HTTP request with a 503 (its message as `detail`,
    and `Retry-After` when it has one) and closes WebSockets with 1013 (try again later), except:

        app.add_middleware(
            MaintenanceMiddleware,
            maintenance=maintenance,
            allow_paths=("/health", "/admin"),
            allow=lambda request: client_ip(request) in OFFICE,
        )

    :param maintenance: the switch.
    :param allow_paths: path prefixes that are always served (`/health` covers `/health/live`),
        matched like the app's routes: without the `root_path` a proxy or a mount adds in front.
    :param allow: decides whether other requests go through during maintenance.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        maintenance: FastAPIMaintenance,
        allow_paths: Iterable[str] = ("/health",),
        allow: Allow | None = None,
    ) -> None:
        if isinstance(allow_paths, str):
            raise TypeError("allow_paths is a list of path prefixes, not a single string")
        paths = tuple(allow_paths)
        for prefix in paths:
            if not prefix.startswith("/"):
                raise ValueError(f"path prefixes start with /: {prefix!r}")
        self.app = app
        self.maintenance = maintenance
        self.allow = allow
        self._paths = tuple(prefix.rstrip("/") for prefix in paths)

    def allowed_path(self, path: str) -> bool:
        """Whether `path` is always served."""
        return any(path == base or path.startswith(base + "/") for base in self._paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or self.allowed_path(route_path(scope)):
            await self.app(scope, receive, send)
            return
        state = await self.maintenance.current()
        if not state.enabled or await self._allowed(scope):
            await self.app(scope, receive, send)
            return
        cache_metrics(self.maintenance.cache).count("maintenance.refused")
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": WS_1013_TRY_AGAIN_LATER, "reason": ""})
            return
        headers = {"Cache-Control": "no-store"}
        if state.retry_after is not None:
            headers["Retry-After"] = str(state.retry_after)
        message = state.message or "Service Unavailable"
        await respond(scope, send, HTTP_503_SERVICE_UNAVAILABLE, message, headers)

    async def _allowed(self, scope: Scope) -> bool:
        if self.allow is None:
            return False
        verdict = self.allow(HTTPConnection(scope))
        if isawaitable(verdict):
            verdict = await verdict
        return bool(verdict)
