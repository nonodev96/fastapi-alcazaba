"""Rate limiting on the cache's backend: how many requests each client may make to a route.

The client's address is the one the ASGI server reports, never `X-Forwarded-For`, which any
client can set to get a fresh budget on every request; a route has one budget whatever its path
parameters, not one per URL (`/items/1`, `/items/2`...); and every 429 says when to retry.
"""

import hashlib
import ipaddress
import json
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Mapping
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

from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.asgi import RATE_LIMIT_HEADERS
from fastapi_alcazaba.core.errors import BackendUnavailable, CircuitOpen
from fastapi_alcazaba.response_cache.caches import Cache, get_cache
from fastapi_alcazaba.telemetry.tracing import add_event

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: Who is making a request: receives the request (or WebSocket) and returns a string.
Identifier = Callable[[HTTPConnection], Awaitable[str] | str]
#: What to do with a request over the limit: receives it and the seconds to wait.
OnLimit = Callable[[HTTPConnection, float], Awaitable[None] | None]
#: How many requests a request's client may make per window: a number, or one per request (by
#: plan, by API key tier).
Limit = int | Callable[[HTTPConnection], Awaitable[int] | int]
#: Who is exempt: identities (what `identifier` returns), or a predicate of the request.
Exemption = Collection[str] | Callable[[HTTPConnection], Awaitable[bool] | bool]

#: Clients over the limit each FastAPIRateLimiter remembers, to refuse them without asking the
#: backend.
_MAX_BLOCKED = 10_000
#: Exempt identities each FastAPIRateLimiter has logged already (once each).
_MAX_LOGGED_EXEMPTIONS = 1_000


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


async def _delete(cache: Cache, key: str) -> None:
    backend = shared_backend(cache.backend)
    await cache.guard.run("delete", lambda: backend.clear(key=key))


def _record(cache: Cache, outcome: str, limiter: str) -> None:
    cache.metrics.rate_limit(outcome, limiter)
    if outcome != "allowed":  # what explains a 429 or an unchecked request in its trace
        add_event("fastapi_alcazaba.rate_limit", outcome=outcome, limiter=limiter)


def too_many_requests(
    connection: HTTPConnection,
    retry_after: float,
    headers: Mapping[str, str] | None = None,
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


def _route_scope(connection: HTTPConnection) -> tuple[str, str]:
    """The route that matched, as (its name in the metrics, what its budget is kept under): its
    methods and path template, so that `/items/1` and `/items/2` share a budget, and for the
    budget its endpoint too. Not the `root_path`: under `Mount("/tenants/{tenant}")` it holds the
    tenant, and each made-up one would get a budget of its own; the endpoint keeps the routes of
    two mounted apps apart. Without a route (called outside the routing), the path."""
    route = connection.scope.get("route")
    path = getattr(route, "path_format", None)
    if not isinstance(path, str):
        raw = str(connection.scope.get("path", ""))
        return raw, raw
    methods = getattr(route, "methods", None)
    label = f"{','.join(sorted(methods)) if methods else 'WS'} {path}"
    endpoint = getattr(route, "endpoint", None)
    name = f"{getattr(endpoint, '__module__', '')}.{getattr(endpoint, '__qualname__', '')}"
    return label, f"{label} {name}"


def _wait(
    times: int,
    window: float,
    elapsed: float,
    previous: int,
    current: int,
    cost: int = 1,
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
    limit: int  # the requests (or cost units) per window of this request's client
    remaining: int  # what fits now, in requests (or cost units)
    reset: int  # seconds during which no more than `remaining` is sure to fit
    retry_after: float | None  # seconds; None when it was allowed


@dataclass(frozen=True, slots=True)
class RateLimitStatus:
    """A client's budget, as `FastAPIRateLimiter.status` reads it without spending any."""

    #: Requests (or cost units) per window.
    limit: int
    #: What fits now.
    remaining: int
    #: Seconds during which no more than `remaining` is sure to fit.
    reset: int


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

    :param times: requests allowed per window; or a function of the request (sync or async)
        that says how many, e.g. by the user's plan. The count is kept whatever it says: a client
        whose plan changes keeps what it spent.
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
    :param exempt: who isn't limited: identities (as `identifier` returns them: an internal
        service's address, an admin's id), or a predicate of the request (sync or async). Their
        requests aren't counted nor get the headers; each is counted as `exempt` in the metrics,
        and logged the first time this process sees the identity.
    """

    def __init__(
        self,
        times: Limit = 1,
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
        exempt: Exemption | None = None,
    ) -> None:
        window = milliseconds + 1000 * (seconds + 60 * (minutes + 60 * hours))
        if not callable(times) and times < 1:
            raise ValueError("times must be at least 1")
        if window < 1:
            raise ValueError("the window must last at least 1 millisecond")
        if isinstance(exempt, str):
            raise TypeError("exempt is a collection of identities, or a predicate: not a string")
        self.times = times
        self.exempt = exempt if exempt is None or callable(exempt) else frozenset(exempt)
        self._logged_exemptions: OrderedDict[str, None] = OrderedDict()
        #: In milliseconds.
        self.window = window
        self.identifier = identifier or client_ip
        self.callback = callback or too_many_requests
        self.name = name
        self.cache = cache
        self.fail_open = fail_open
        self.cost = cost if callable(times) else self._check_cost(cost, times)
        if cost < 1:
            raise ValueError("cost must be at least 1")
        self.headers = headers
        if name is not None:
            policy = name
        elif callable(times):
            policy = f"per-{_duration(window)}"
        else:
            policy = f"{times}/{_duration(window)}"
        if headers and not all(" " <= char <= "~" for char in policy):
            raise ValueError("name must be printable ASCII to go in the RateLimit headers")
        self._policy = _sf_string(policy)
        # Part of the counters' keys: limiters of one name and rate share them; a limit computed
        # per request keeps one count whatever it says.
        self._rate = f"{'dynamic' if callable(times) else times}/{window}"
        # A window's counter is read during the next one; one more second for clock differences.
        self._expire = math.ceil(2 * window / 1000) + 1
        # Counter key -> until when nothing fits (ms), and the window's index and counts then.
        self._blocked: OrderedDict[str, tuple[float, int, int, int]] = OrderedDict()
        self._source: Cache | None = None  # the cache whose counters _blocked comes from

    def forget(self) -> None:
        """Forget the clients this process refuses without asking the backend (those with nothing
        left): their next requests ask it again. The counters in the backend stay. It's done by
        itself when the cache changes, as each test's lifespan sets one up."""
        self._blocked.clear()

    @staticmethod
    def _check_cost(cost: int, times: int) -> int:
        if not 1 <= cost <= times:
            raise ValueError(f"cost must be between 1 and times ({times})")
        return cost

    async def _limit(self, connection: HTTPConnection | None) -> int:
        """This request's `times`."""
        if not callable(self.times):
            return self.times
        if connection is None:
            raise ValueError("a limit computed per request needs the request")
        found = self.times(connection)
        limit = await found if isawaitable(found) else found
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError(f"the limit of the request must be an int of 1 or more: {limit!r}")
        return limit

    async def _exempt(self, connection: HTTPConnection, identity: str) -> bool:
        if self.exempt is None:
            return False
        if callable(self.exempt):
            found = self.exempt(connection)
            exempt = bool(await found if isawaitable(found) else found)
        else:
            exempt = identity in self.exempt
        if exempt and identity not in self._logged_exemptions:
            self._logged_exemptions[identity] = None
            if len(self._logged_exemptions) > _MAX_LOGGED_EXEMPTIONS:
                self._logged_exemptions.popitem(last=False)
            logger.info("Rate limiter of %s: %r is exempt", self.name or "its route", identity)
        return exempt

    async def __call__(self, connection: HTTPConnection, response: Response) -> None:
        outcome = await self._take(connection, "", self.cost)
        if outcome is None:
            return
        headers = self._headers(outcome) if self.headers else {}
        if outcome.retry_after is not None:
            if self.callback is too_many_requests:  # the default, with the RateLimit headers
                too_many_requests(connection, outcome.retry_after, headers)
            elif isawaitable(reaction := self.callback(connection, outcome.retry_after)):
                await reaction
        for header, value in headers.items():
            # Appended: each limiter of the route adds its policy to the list.
            response.headers.append(header, value)
        if headers:  # not for a refusal: its 429 carries them already
            state = connection.scope.setdefault("state", {})
            state.setdefault(RATE_LIMIT_HEADERS, []).extend(headers.items())

    def _wait_since(
        self,
        times: int,
        now: float,
        counts: tuple[int, int, int],
        cost: int,
    ) -> float:
        """`_wait` for `cost`, from the counts of window `index` seen by an earlier request."""
        index, previous, current = counts
        now_index, elapsed = divmod(now, self.window)
        if now_index == index:
            return _wait(times, self.window, elapsed, previous, current, cost)
        if now_index == index + 1:  # that window is the previous one now
            return _wait(times, self.window, elapsed, current, 0, cost)
        return 0.0

    def _headers(self, outcome: _Outcome) -> dict[str, str]:
        window = f";w={self.window // 1000}" if self.window % 1000 == 0 else ""
        return {
            "RateLimit": f"{self._policy};r={outcome.remaining};t={outcome.reset}",
            "RateLimit-Policy": f"{self._policy};q={outcome.limit}{window}",
        }

    async def hit(
        self,
        connection: HTTPConnection,
        key: str = "",
        cost: int | None = None,
    ) -> float | None:
        """Count a request: None if it's within the limit, else the seconds until it would be
        (and it isn't counted). Call it yourself to limit something else than whole requests:
        each message of a WebSocket, or the login attempts to an account (`key=username`).
        `cost` is how much of the budget it takes (the limiter's `cost` by default)."""
        if cost is not None and cost < 1:
            raise ValueError("cost must be at least 1")
        outcome = await self._take(connection, key, self.cost if cost is None else cost)
        return None if outcome is None else outcome.retry_after

    async def reset(
        self,
        connection: HTTPConnection | None = None,
        *,
        identity: str | None = None,
        key: str = "",
        cache: Cache | None = None,
    ) -> None:
        """Forget what a client spent: after a successful login, the failed attempts
        (`key=account`). The client is the request's (`identifier`), or `identity`; without a
        request, the limiter needs a `name` (its budget is the route's otherwise), and a cache.
        Other processes that refuse the client without asking the backend (it had nothing left)
        go on doing so until that budget would have refilled."""
        found, base, now = await self._locate(connection, identity, key, cache)
        index = int(now // self.window)
        for window in (index, index - 1):
            await _delete(found, f"{base}:{window}")
        self._blocked.pop(base, None)

    async def status(
        self,
        connection: HTTPConnection | None = None,
        *,
        identity: str | None = None,
        key: str = "",
        cache: Cache | None = None,
    ) -> RateLimitStatus:
        """A client's budget without spending any: its limit, what fits now, and for how long no
        more is sure to fit. The client as for `reset`. Raises BackendUnavailable when the
        backend fails."""
        found, base, now = await self._locate(connection, identity, key, cache)
        times = await self._limit(connection)
        index, elapsed = divmod(now, self.window)
        backend = shared_backend(found.backend)
        current = await found.guard.run("get", lambda: backend.get_counter(f"{base}:{int(index)}"))
        previous = await found.guard.run(
            "get", lambda: backend.get_counter(f"{base}:{int(index) - 1}")
        )
        weight = (self.window - elapsed) / self.window
        remaining = max(0, math.floor(times - previous * weight - current))
        if remaining:
            reset = math.ceil((self.window - elapsed) / 1000)
        else:
            reset = math.ceil(_wait(times, self.window, elapsed, previous, current) / 1000)
        return RateLimitStatus(times, remaining, reset)

    async def _locate(
        self,
        connection: HTTPConnection | None,
        identity: str | None,
        key: str,
        cache: Cache | None,
    ) -> tuple[Cache, str, float]:
        """The cache, the base key of the client's counters, and now (ms)."""
        if connection is None and (self.name is None or identity is None):
            raise ValueError(
                "without the request, give the identity, and the limiter a name: its budget is "
                "the route's otherwise"
            )
        found = cache or self.cache or (get_cache(connection) if connection is not None else None)
        if found is None:
            raise ValueError("without the request, give the cache")
        if self.name is not None:
            budget = self.name
        else:
            assert connection is not None  # noqa: S101  # checked above
            budget = _route_scope(connection)[1]
        if identity is None:
            assert connection is not None  # noqa: S101  # checked above
            identity = await self._identity(connection)
        return found, self._base(found, budget, identity, key), time.time() * 1000

    async def _identity(self, connection: HTTPConnection) -> str:
        identity = self.identifier(connection)
        return str(await identity if isawaitable(identity) else identity)

    def _base(self, cache: Cache, budget: str, identity: str, key: str) -> str:
        parts = [budget, self._rate, identity, key]
        digest = hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32]
        # The {hash tag} puts both windows of a client in the same Redis Cluster slot.
        return cache.state_key("ratelimit", f"{{{digest}}}")

    async def _take(self, connection: HTTPConnection, key: str, cost: int) -> _Outcome | None:
        """Count `cost`; None when the backend failed and requests go through unchecked."""
        cache = self.cache if self.cache is not None else get_cache(connection)
        if cache is not self._source:
            self._source = cache
            self.forget()
        backend = cache.backend
        if not backend.supports_counters:
            raise NotImplementedError(
                f"{type(backend).__name__} can't keep counters, which the rate limiter needs"
            )
        scope, budget = (
            (self.name, self.name) if self.name is not None else _route_scope(connection)
        )
        identity = await self._identity(connection)
        if await self._exempt(connection, identity):
            _record(cache, "exempt", scope)
            return None
        times = await self._limit(connection)
        if cost > times:
            raise ValueError(f"cost ({cost}) is more than the request's limit ({times})")
        base = self._base(cache, budget, identity, key)
        now = time.time() * 1000
        if (blocked := self._blocked.get(base)) is not None:
            until, blocked_index, blocked_previous, blocked_current = blocked
            # No block lasts two windows: a longer one means the clock was stepped back.
            if now < until <= now + 2 * self.window:
                _record(cache, "limited", scope)
                counts = (blocked_index, blocked_previous, blocked_current)
                wait = max(until - now, self._wait_since(times, now, counts, cost))
                return _Outcome(times, 0, math.ceil((until - now) / 1000), wait / 1000)
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
                    limit=times,
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
        remaining = max(0, math.floor(times - previous * weight - current))
        if remaining:
            # That much fits until this window ends, and more as the previous one fades.
            reset = math.ceil((self.window - elapsed) / 1000)
        else:
            refill = _wait(times, self.window, elapsed, previous, current)
            reset = math.ceil(refill / 1000)
        if added:
            _record(cache, "allowed", scope)
            return _Outcome(times, remaining, reset, None)
        _record(cache, "limited", scope)
        if not remaining:
            # Nothing fits until then, whatever its cost and whatever other requests come.
            self._blocked[base] = (now + refill, int(index), previous, current)
            if len(self._blocked) > _MAX_BLOCKED:
                self._blocked.popitem(last=False)
        wait = _wait(times, self.window, elapsed, previous, current, cost)
        return _Outcome(times, remaining, reset, wait / 1000)
