"""Rate limiting on the cache's backend: how many requests each client may make to a route.

A port of fastapi-limiter (by long2ice, the author of fastapi-cache) to Alcazaba's backends,
without its weaknesses: it took the client's address from `X-Forwarded-For`, which any client can
set to get a fresh budget on every request; it counted each URL apart, so `/items/1`, `/items/2`...
had a budget each; and its 429s didn't say when to retry.
"""

import hashlib
import ipaddress
import json
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from inspect import isawaitable
from typing import NoReturn

from fastapi import HTTPException, WebSocketException
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.status import (
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_503_SERVICE_UNAVAILABLE,
    WS_1008_POLICY_VIOLATION,
    WS_1013_TRY_AGAIN_LATER,
)

from fastapi_alcazaba.cache import Cache, get_cache
from fastapi_alcazaba.errors import BackendUnavailable, CircuitOpen
from fastapi_alcazaba.tracing import add_event

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: Who is making a request: receives the request (or WebSocket) and returns a string.
Identifier = Callable[[HTTPConnection], Awaitable[str] | str]
#: What to do with a request over the limit: receives it and the seconds to wait.
OnLimit = Callable[[HTTPConnection, float], Awaitable[None] | None]

#: Clients over the limit each FastAPIRateLimiter remembers, to refuse them without asking the
#: backend.
_MAX_BLOCKED = 10_000


def _network(host: str, ipv4_prefix: int, ipv6_prefix: int) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host  # not an address: a Unix socket, a test client
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is None:
            return str(ipaddress.IPv6Network((address, ipv6_prefix), strict=False))
        address = address.ipv4_mapped
    if ipv4_prefix == 32:
        return str(address)
    return str(ipaddress.IPv4Network((address, ipv4_prefix), strict=False))


def client_ip(connection: HTTPConnection) -> str:
    """The client's address as the ASGI server reports it, IPv6 addresses by their /64 network:
    that's what an ISP gives a single customer, who can use any of its 2⁶⁴ addresses. Behind a
    proxy, let the server take it from the proxy's headers (`uvicorn --proxy-headers
    --forwarded-allow-ips=<proxy address>`): read here, `X-Forwarded-For` would let every client
    choose who it is."""
    return _network(connection.client.host if connection.client else "", 32, 64)


def client_network(ipv4_prefix: int = 32, ipv6_prefix: int = 64) -> Identifier:
    """An identifier like `client_ip` with other network sizes: `client_network(24, 48)` counts a
    whole IPv4 /24 or IPv6 /48 as one client."""
    if not (0 <= ipv4_prefix <= 32 and 0 <= ipv6_prefix <= 128):
        raise ValueError("the prefixes must be 0-32 (IPv4) and 0-128 (IPv6)")

    def identify(connection: HTTPConnection) -> str:
        host = connection.client.host if connection.client else ""
        return _network(host, ipv4_prefix, ipv6_prefix)

    return identify


def _record(cache: Cache, outcome: str, limiter: str) -> None:
    cache.metrics.rate_limit(outcome, limiter)
    if outcome != "allowed":  # what explains a 429 or an unchecked request in its trace
        add_event("fastapi_alcazaba.rate_limit", outcome=outcome, limiter=limiter)


def too_many_requests(
    connection: HTTPConnection, retry_after: float, headers: Mapping[str, str] | None = None
) -> NoReturn:
    """The default reaction to a request over the limit: a 429 with `Retry-After` (and `headers`),
    or closing a WebSocket with 1008 (policy violation)."""
    if connection.scope["type"] == "websocket":
        raise WebSocketException(WS_1008_POLICY_VIOLATION, "Too Many Requests")
    raise HTTPException(
        HTTP_429_TOO_MANY_REQUESTS,
        "Too Many Requests",
        headers={**(headers or {}), "Retry-After": str(math.ceil(retry_after))},
    )


def _route_scope(connection: HTTPConnection) -> str:
    """The route that matched: its methods and path template, so that `/items/1` and `/items/2`
    share a budget. Without a route (called outside the routing), the path."""
    route = connection.scope.get("route")
    path = getattr(route, "path_format", None)
    if not isinstance(path, str):
        return str(connection.scope.get("path", ""))
    methods = getattr(route, "methods", None)
    verbs = ",".join(sorted(methods)) if methods else "WS"
    return f"{verbs} {connection.scope.get('root_path', '')}{path}"


def _wait(
    times: int, window: float, elapsed: float, previous: int, current: int, cost: int = 1
) -> float:
    """Time until `cost` more fits, if nothing else comes; `previous` and `current` are the counts
    of the previous and the current window without it."""
    if current + cost <= times:
        # The current window has room: wait until the previous one weighs little enough.
        if previous <= 0:
            return 0.0
        return max(0.0, window - elapsed - (times - current - cost) * window / previous)
    # Wait for the next window, in which this one is the previous one.
    return window - elapsed + window * (1 - (times - cost) / current)


def _duration(milliseconds: int) -> str:
    return f"{milliseconds // 1000}s" if milliseconds % 1000 == 0 else f"{milliseconds}ms"


def _sf_string(text: str) -> str:
    """A Structured Field string (RFC 9651)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True, slots=True)
class _Outcome:
    remaining: int  # what fits now, in requests (or cost units)
    reset: int  # seconds during which no more than `remaining` is sure to fit
    retry_after: float | None  # seconds; None when it was allowed


class FastAPIRateLimiter:
    """FastAPI dependency that lets each client make `times` requests per window to a route; the
    next ones get a 429 with `Retry-After` until the window slides past enough of them.

        @app.get("/search", dependencies=[Depends(FastAPIRateLimiter(times=10, minutes=1))])

    The counters live in the cache's backend (the `cache` given, else the one `get_cache`
    returns), so every process shares them, and go through its timeout and circuit breaker. Their
    keys are SHA-256 digests: identities never reach the backend in clear.

    It's a sliding window: the count of the previous window is added in proportion to how much of
    it is still less than a window ago, so no client doubles its rate around the boundary between
    two windows. That assumes the previous window's requests were spread evenly; when they came at
    its end, it's a little stricter than `times` per window. Requests over the limit don't count:
    checking and counting is one atomic operation where the backend allows it. Each process then
    refuses a client with nothing left without asking the backend, until something fits again, so
    a flood doesn't reach the backend.

    Responses carry the `RateLimit` and `RateLimit-Policy` headers of the IETF draft
    (draft-ietf-httpapi-ratelimit-headers), for clients to slow down before they get a 429.

    :param times: requests allowed per window.
    :param milliseconds: the window is the sum of `milliseconds`, `seconds`, `minutes` and `hours`.
    :param identifier: who the client is; its address by default (`client_ip`). E.g. a user id.
    :param callback: called with the request and the seconds to wait when a request is over the
        limit; `too_many_requests` by default. If it returns, the request goes on.
    :param name: the routes using limiters with this name and the same rate share one budget; by
        default each route (its methods and path template) has its own. Also the policy's name in
        the headers.
    :param cache: the Cache whose backend keeps the counters.
    :param fail_open: when the backend fails, let requests through (the default, as the cache
        does) or refuse them with a 503.
    :param cost: how much of the budget each request takes: an expensive route can take more of a
        budget it shares with others (`name`).
    :param headers: send the `RateLimit` and `RateLimit-Policy` headers.
    """

    def __init__(
        self,
        times: int = 1,
        milliseconds: int = 0,
        seconds: int = 0,
        minutes: int = 0,
        hours: int = 0,
        identifier: Identifier | None = None,
        callback: OnLimit | None = None,
        *,
        name: str | None = None,
        cache: Cache | None = None,
        fail_open: bool = True,
        cost: int = 1,
        headers: bool = True,
    ) -> None:
        window = milliseconds + 1000 * (seconds + 60 * (minutes + 60 * hours))
        if times < 1:
            raise ValueError("times must be at least 1")
        if window < 1:
            raise ValueError("the window must last at least 1 millisecond")
        self.times = times
        #: In milliseconds.
        self.window = window
        self.identifier = identifier or client_ip
        self.callback = callback or too_many_requests
        self.name = name
        self.cache = cache
        self.fail_open = fail_open
        self.cost = self._check_cost(cost)
        self.headers = headers
        policy = name if name is not None else f"{times}/{_duration(window)}"
        if headers and not all(" " <= char <= "~" for char in policy):
            raise ValueError("name must be printable ASCII to go in the RateLimit headers")
        self._policy = _sf_string(policy)
        self._policy_header = f"{self._policy};q={times}" + (
            f";w={window // 1000}" if window % 1000 == 0 else ""
        )
        # A window's counter is read during the next one; one more second for clock differences.
        self._expire = math.ceil(2 * window / 1000) + 1
        # Counter key -> until when nothing fits (ms), and the window's index and counts then.
        self._blocked: OrderedDict[str, tuple[float, int, int, int]] = OrderedDict()

    def _check_cost(self, cost: int) -> int:
        if not 1 <= cost <= self.times:
            raise ValueError(f"cost must be between 1 and times ({self.times})")
        return cost

    async def __call__(self, connection: HTTPConnection, response: Response) -> None:
        outcome = await self._take(connection, "", self.cost)
        if outcome is None:
            return
        headers = self._headers(outcome) if self.headers else {}
        if outcome.retry_after is not None:
            if self.callback is too_many_requests:
                too_many_requests(connection, outcome.retry_after, headers)
            reaction = self.callback(connection, outcome.retry_after)
            if isawaitable(reaction):
                await reaction
        for header, value in headers.items():
            # Appended: each limiter of the route adds its policy to the list.
            response.headers.append(header, value)

    def _wait_since(self, now: float, index: int, previous: int, current: int, cost: int) -> float:
        """`_wait` for `cost`, from the counts of window `index` seen by an earlier request."""
        now_index, elapsed = divmod(now, self.window)
        if now_index == index:
            return _wait(self.times, self.window, elapsed, previous, current, cost)
        if now_index == index + 1:  # that window is the previous one now
            return _wait(self.times, self.window, elapsed, current, 0, cost)
        return 0.0

    def _headers(self, outcome: _Outcome) -> dict[str, str]:
        return {
            "RateLimit": f"{self._policy};r={outcome.remaining};t={outcome.reset}",
            "RateLimit-Policy": self._policy_header,
        }

    async def hit(
        self, connection: HTTPConnection, key: str = "", cost: int | None = None
    ) -> float | None:
        """Count a request: None if it's within the limit, else the seconds until it would be
        (and it isn't counted). Call it yourself to limit something else than whole requests:
        each message of a WebSocket, or the login attempts to an account (`key=username`).
        `cost` is how much of the budget it takes (the limiter's `cost` by default)."""
        cost = self.cost if cost is None else self._check_cost(cost)
        outcome = await self._take(connection, key, cost)
        return None if outcome is None else outcome.retry_after

    async def _take(self, connection: HTTPConnection, key: str, cost: int) -> _Outcome | None:
        """Count `cost`; None when the backend failed and requests go through unchecked."""
        cache = self.cache if self.cache is not None else get_cache(connection)
        backend = cache.backend
        if not backend.supports_counters:
            raise NotImplementedError(
                f"{type(backend).__name__} can't keep counters, which the rate limiter needs"
            )
        scope = self.name if self.name is not None else _route_scope(connection)
        identity = self.identifier(connection)
        if isawaitable(identity):
            identity = await identity
        parts = [scope, f"{self.times}/{self.window}", str(identity), key]
        digest = hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32]
        # The {hash tag} puts both windows of a client in the same Redis Cluster slot.
        base = cache.state_key("ratelimit", f"{{{digest}}}")
        now = time.time() * 1000
        if (blocked := self._blocked.get(base)) is not None:
            until, blocked_index, blocked_previous, blocked_current = blocked
            # No block lasts two windows: a longer one means the clock was stepped back.
            if now < until <= now + 2 * self.window:
                _record(cache, "limited", scope)
                counts = (blocked_index, blocked_previous, blocked_current)
                wait = max(until - now, self._wait_since(now, *counts, cost))
                return _Outcome(0, math.ceil((until - now) / 1000), wait / 1000)
            del self._blocked[base]
        index, elapsed = divmod(now, self.window)
        weight = (self.window - elapsed) / self.window
        try:
            added, current, previous = await cache.guard.run(
                "incr_window",
                lambda: backend.incr_window(
                    f"{base}:{int(index)}",
                    f"{base}:{int(index) - 1}",
                    amount=cost,
                    limit=self.times,
                    weight=weight,
                    expire=self._expire,
                ),
            )
        except BackendUnavailable as exc:
            _record(cache, "unchecked", scope)
            if isinstance(exc, CircuitOpen):
                logger.debug("Rate limiter of %s not checked: %s", scope, exc)
            else:
                logger.warning("Rate limiter of %s not checked", scope, exc_info=exc)
            if self.fail_open:
                return None
            if connection.scope["type"] == "websocket":
                raise WebSocketException(WS_1013_TRY_AGAIN_LATER) from exc
            raise HTTPException(HTTP_503_SERVICE_UNAVAILABLE) from exc
        remaining = max(0, math.floor(self.times - previous * weight - current))
        if remaining:
            # That much fits until this window ends, and more as the previous one fades.
            reset = math.ceil((self.window - elapsed) / 1000)
        else:
            refill = _wait(self.times, self.window, elapsed, previous, current)
            reset = math.ceil(refill / 1000)
        if added:
            _record(cache, "allowed", scope)
            return _Outcome(remaining, reset, None)
        _record(cache, "limited", scope)
        if not remaining:
            # Nothing fits until then, whatever its cost and whatever other requests come.
            self._blocked[base] = (now + refill, int(index), previous, current)
            if len(self._blocked) > _MAX_BLOCKED:
                self._blocked.popitem(last=False)
        wait = _wait(self.times, self.window, elapsed, previous, current, cost)
        return _Outcome(remaining, reset, wait / 1000)
