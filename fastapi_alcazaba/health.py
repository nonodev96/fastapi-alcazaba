"""Liveness and readiness endpoints for load balancers and orchestrators (Kubernetes, ECS...).

`/live` only says that the process answers; `/ready` runs the app's checks (database, cache...)
and says whether it can serve. They're apart on purpose: a liveness probe that checks
dependencies gets healthy containers restarted every time the database blips.

Probes are frequent and the endpoints are public, so concurrent `/ready` requests share one run
of the checks, its result is reused for a moment, and errors are logged, never returned: a
probe must not tell the world the database's hostname or connection string.
"""

import asyncio
import contextlib
import contextvars
import functools
import inspect
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE

from fastapi_alcazaba.cache import Cache, get_cache
from fastapi_alcazaba.types import Backend

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: A readiness check: passes when it returns anything but False without raising.
Check = Callable[[], Awaitable[object] | object]

_NO_STORE = {"Cache-Control": "no-store"}


@dataclass(frozen=True)
class HealthCheck:
    """A readiness check with its options.

    :param check: sync or async callable; it passes when it returns anything but False without
        raising within `timeout`. Sync ones run in a thread of their own, off the event loop.
    :param critical: when it fails, the app isn't ready (503); a non-critical one only makes it
        "degraded" (200), for dependencies the app can work without.
    :param timeout: seconds it may take; the router's `timeout` by default.
    """

    check: Check
    critical: bool = True
    timeout: float | None = None

    def __post_init__(self) -> None:
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("timeout must be positive")


def _is_async(check: Check) -> bool:
    # A function, or an object with an async __call__.
    return inspect.iscoroutinefunction(check) or inspect.iscoroutinefunction(type(check).__call__)


async def _call(check: Check, name: str, threads: dict[str, threading.Thread]) -> object:
    if _is_async(check):
        result = check()
    else:
        if (previous := threads.get(name)) is not None and previous.is_alive():
            # Another thread for a check that hangs would pile up with every probe.
            raise RuntimeError("still running since an earlier probe")
        result = await _in_thread(check, name, threads)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _in_thread(check: Check, name: str, threads: dict[str, threading.Thread]) -> object:
    """Runs a sync check in a daemon thread of its own, abandoned at its timeout.

    Not starlette's run_in_threadpool: anyio waits for the thread when the call is cancelled, so
    a hanging check would outlive its timeout. Nor the loop's default executor: asyncio.run
    (uvicorn) waits for its threads at shutdown, and hanging checks would hold the threads that
    `loop.getaddrinfo` needs.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[object] = loop.create_future()
    # Retrieved even when nobody waits any more (a check that fails after its timeout): asyncio
    # would log "Future exception was never retrieved" otherwise. The failure was logged already.
    future.add_done_callback(lambda done: done.cancelled() or done.exception())
    context = contextvars.copy_context()

    def run() -> None:
        try:
            settle = functools.partial(future.set_result, context.run(check))
        except Exception as exc:
            settle = functools.partial(future.set_exception, exc)
        with contextlib.suppress(RuntimeError):  # the loop is closed: nobody is waiting any more
            loop.call_soon_threadsafe(settle)

    threads[name] = thread = threading.Thread(target=run, name=f"health check {name}", daemon=True)
    thread.start()
    # Shielded: the timeout cancels the wait, not the future the thread settles.
    return await asyncio.shield(future)


async def _run_check(
    name: str, health_check: HealthCheck, default: float, threads: dict[str, threading.Thread]
) -> dict[str, Any]:
    seconds = health_check.timeout if health_check.timeout is not None else default
    start = time.perf_counter()
    deadline = asyncio.timeout(seconds)
    passed = False
    try:
        async with deadline:
            passed = await _call(health_check.check, name, threads) is not False
        if not passed:
            logger.warning("Health check %r returned False", name)
    except Exception:
        if deadline.expired():
            logger.warning("Health check %r didn't answer within %ss", name, seconds)
        else:
            logger.warning("Health check %r failed", name, exc_info=True)
    duration = round((time.perf_counter() - start) * 1000, 3)
    return {"status": "ok" if passed else "fail", "duration_ms": duration}


def _cache_check(backend: Backend, prefix: str) -> Callable[[], Awaitable[object]]:
    async def cache() -> object:
        # Straight to the backend, not through the circuit breaker: an open breaker would hide
        # that the backend is back. The key is under the cache's prefix, which ACLs may require.
        return await backend.get(f"{prefix}:fastapi_alcazaba:health")

    return cache


class _Readiness:
    def __init__(
        self,
        checks: dict[str, HealthCheck],
        cache: Cache | None,
        include_cache: bool,
        timeout: float,
        reuse_for: float,
    ) -> None:
        self.checks = checks
        self.cache = cache
        self.include_cache = include_cache
        self.timeout = timeout
        self.reuse_for = reuse_for
        self._running: asyncio.Future[tuple[int, dict[str, Any]]] | None = None
        self._last: tuple[float, int, dict[str, Any]] | None = None  # (when, status, body)
        # Sync checks' threads by name: abandoned at their timeout, they may still be running.
        self._threads: dict[str, threading.Thread] = {}

    def _cache(self, request: Request) -> Cache | None:
        if not self.include_cache:
            return None
        if self.cache is not None:
            return self.cache
        try:
            return get_cache(request)
        except RuntimeError:  # no cache configured at all: nothing to check
            return None

    async def respond(self, request: Request) -> JSONResponse:
        """Whether the app can serve: 200 when every critical check passes, else 503."""
        if (last := self._last) is not None and time.monotonic() - last[0] < self.reuse_for:
            return JSONResponse(last[2], last[1], headers=_NO_STORE)
        running = self._running
        if running is None or running.done():
            running = self._running = asyncio.ensure_future(self._run(self._cache(request)))
        # Shielded: a request that goes away doesn't cancel the run the others are waiting for.
        status, body = await asyncio.shield(running)
        return JSONResponse(body, status, headers=_NO_STORE)

    async def _run(self, cache: Cache | None) -> tuple[int, dict[str, Any]]:
        checks = dict(self.checks)
        if cache is not None:
            # Not critical: the cache fails open, so its outage mustn't take every instance out
            # of the load balancer.
            checks["cache"] = HealthCheck(_cache_check(cache.backend, cache.prefix), critical=False)
        names = list(checks)
        results = await asyncio.gather(
            *(
                _run_check(name, check, self.timeout, self._threads)
                for name, check in checks.items()
            )
        )
        outcome = dict(zip(names, results, strict=True))
        failed = [name for name, result in outcome.items() if result["status"] == "fail"]
        if any(checks[name].critical for name in failed):
            status, summary = HTTP_503_SERVICE_UNAVAILABLE, "fail"
        else:
            status, summary = HTTP_200_OK, "degraded" if failed else "ok"
        body = {"status": summary, "checks": outcome}
        self._last = (time.monotonic(), status, body)
        return status, body


async def _live() -> JSONResponse:
    """The process answers. No checks: a database outage mustn't get the app restarted."""
    return JSONResponse({"status": "ok"}, headers=_NO_STORE)


def health_router(
    checks: Mapping[str, Check | HealthCheck] | None = None,
    *,
    cache: Cache | None = None,
    include_cache: bool = True,
    prefix: str = "/health",
    timeout: float = 2.0,
    reuse_for: float = 1.0,
    include_in_schema: bool = False,
    tags: list[str | Enum] | None = None,
) -> APIRouter:
    """`GET {prefix}/live` and `GET {prefix}/ready`, to include in the app.

        app.include_router(health_router({"database": check_database}))

    :param checks: readiness checks by name: callables (critical) or `HealthCheck`s.
    :param cache: the Cache whose backend `/ready` checks; else the one `get_cache` returns, if
        any is configured.
    :param include_cache: add the non-critical "cache" check.
    :param prefix: path prefix of both endpoints ("" for none).
    :param timeout: seconds each check may take, unless its HealthCheck says otherwise.
    :param reuse_for: seconds during which a result answers the next probes; 0 runs the checks
        for every probe (concurrent probes still share one run).
    :param include_in_schema: show the endpoints in the OpenAPI schema.
    :param tags: OpenAPI tags of the endpoints.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if reuse_for < 0:
        raise ValueError("reuse_for can't be negative")
    if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
        raise ValueError("prefix must start with '/' and not end with one (or be empty)")
    normalised = {
        name: check if isinstance(check, HealthCheck) else HealthCheck(check)
        for name, check in (checks or {}).items()
    }
    if include_cache and "cache" in normalised:
        raise ValueError(
            "'cache' is the name of the built-in cache check: rename yours or pass "
            "include_cache=False"
        )
    readiness = _Readiness(normalised, cache, include_cache, timeout, reuse_for)
    router = APIRouter(prefix=prefix, tags=tags, include_in_schema=include_in_schema)
    router.add_api_route("/live", _live, methods=["GET"], name="health_live")
    router.add_api_route(
        "/ready",
        readiness.respond,
        methods=["GET"],
        name="health_ready",
        responses={HTTP_503_SERVICE_UNAVAILABLE: {"description": "A critical check failed"}},
    )
    return router
