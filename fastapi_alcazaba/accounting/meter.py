"""Accounting: how much each subject (a user, an API key, a tenant) uses of each meter, per
period, and the quotas that bound it.

    accounting = FastAPIAccounting(
        subject=current_tenant,
        quotas=[Quota("requests", limit=10_000), Quota("tokens", limit=tokens_of_plan)],
    )

    @app.get("/search", dependencies=[Depends(accounting.meter("requests"))])
    async def search(q: str) -> list[Result]: ...

    @app.post("/answers")
    async def answer(
        question: Question,
        usage: Annotated[Metering, Depends(accounting.meter("tokens", amount=None))],
    ) -> Answer:
        answer = await model.complete(question)
        usage.add(answer.tokens)  # charged when the endpoint ends
        return answer

The usage is kept twice, for two purposes:

- Counters in the cache's backend, per subject, meter and period (a calendar hour, day or month,
  in UTC), shared by every process: the quotas read them, and `usage()` tells a subject what it
  has used and what it has left. They keep the current period and the one before; their keys
  are digests, so the subjects never reach the backend in clear.
- Usage records, one per charge, sent to the sinks (the `fastapi_alcazaba.accounting.usage`
  logger by default, or a table of yours): what billing adds up. When the backend fails, the
  counters miss a charge, the records don't.
"""

import asyncio
import hashlib
import json
import logging
import math
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from inspect import isawaitable
from typing import Annotated, Any, Literal, NoReturn, TypeVar, cast, get_args

import anyio
from fastapi import Depends
from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from starlette.exceptions import HTTPException, WebSocketException
from starlette.requests import ClientDisconnect, HTTPConnection, Request
from starlette.responses import Response
from starlette.status import (
    HTTP_401_UNAUTHORIZED,
    HTTP_402_PAYMENT_REQUIRED,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_503_SERVICE_UNAVAILABLE,
    WS_1008_POLICY_VIOLATION,
    WS_1013_TRY_AGAIN_LATER,
)
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba.accounting.records import LoggingUsageSink, UsageRecord, UsageSink
from fastapi_alcazaba.core.asgi import RATE_LIMIT_HEADERS
from fastapi_alcazaba.core.errors import BackendUnavailable, CircuitOpen
from fastapi_alcazaba.observability.request_id import request_id
from fastapi_alcazaba.response_cache.caches import Cache, FastAPICache, get_cache
from fastapi_alcazaba.telemetry.metrics import Metrics
from fastapi_alcazaba.telemetry.tracing import traceparent

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: How long a counter lasts: a calendar hour, day or month, in UTC.
Period = Literal["hour", "day", "month"]
PERIODS: tuple[str, ...] = get_args(Period)

#: A quota's limit: a number of units, or a function of the subject's ID that gives it (its plan's,
#: read from your database); None for no limit.
Limit = int | Callable[[str], int | None | Awaitable[int | None]]

#: Who uses it: a FastAPI dependency that returns the subject's ID (a str, an int or a UUID: a
#: user's, an API key's, a tenant's), or None when nobody is authenticated.
Subject = Callable[..., Any]

T = TypeVar("T")

_METER = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_LONGEST = {"hour": timedelta(hours=1), "day": timedelta(days=1), "month": timedelta(days=31)}
_WINDOW = {"hour": 3600, "day": 86400}  # a month has no fixed length: no `w` in its policy
_MAX_SUBJECT = 200


def _check_meter(meter: str) -> str:
    if not isinstance(meter, str) or not _METER.fullmatch(meter):
        raise ValueError(
            f"a meter is a name of lowercase letters, digits, '_', '.' and '-', not {meter!r}"
        )
    return meter


def _check_period(period: str) -> Period:
    if period not in PERIODS:
        raise ValueError(f"the period is one of {', '.join(PERIODS)}, not {period!r}")
    return cast(Period, period)


def period_of(period: Period, at: datetime) -> tuple[str, datetime, datetime]:
    """The period that holds `at`: its label (`2026-11`, `2026-11-01`, `2026-11-01T10`), when it
    starts and when it ends, in UTC."""
    at = at.astimezone(UTC)
    if period == "hour":
        start = at.replace(minute=0, second=0, microsecond=0)
        return start.strftime("%Y-%m-%dT%H"), start, start + timedelta(hours=1)
    if period == "day":
        start = at.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.strftime("%Y-%m-%d"), start, start + timedelta(days=1)
    start = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start.strftime("%Y-%m"), start, end


@dataclass(frozen=True, slots=True)
class Quota:
    """How much of `meter` a subject may use per `period`.

        Quota("requests", limit=10_000)  # a month, the same for everybody
        Quota("tokens", limit=tokens_of_plan, period="day")  # each subject's plan says

    :param meter: what it bounds, as `FastAPIAccounting.meter()` names it.
    :param limit: the units allowed per period, 0 for none; or `limit(subject)`, a function (or a
        coroutine function) of the subject's ID that gives them, None for no limit.
    :param period: a calendar hour, day or month, in UTC: the counter starts again at 0 when it
        ends.
    :param status: what a request over the quota gets: 429 (Too Many Requests, with
        `Retry-After`: when the period ends) or 402 (Payment Required: a plan to upgrade).
    """

    meter: str
    limit: Limit
    period: Period = "month"
    status: int = HTTP_429_TOO_MANY_REQUESTS

    def __post_init__(self) -> None:
        _check_meter(self.meter)
        _check_period(self.period)
        limit = self.limit
        if isinstance(limit, bool) or not (isinstance(limit, int) or callable(limit)):
            raise TypeError("limit is a number of units, or a function of the subject")
        if isinstance(self.limit, int) and self.limit < 0:
            raise ValueError("limit can't be negative (0 allows nothing)")
        if self.status not in (HTTP_402_PAYMENT_REQUIRED, HTTP_429_TOO_MANY_REQUESTS):
            raise ValueError("status is 429 (Too Many Requests) or 402 (Payment Required)")


@dataclass(frozen=True, slots=True)
class UsageReport:
    """What a subject has used of a meter in a period, and what its quota leaves.

    :param subject: the subject's ID.
    :param meter: the meter.
    :param period: the period's label: `2026-11`, `2026-11-01`, `2026-11-01T10` (UTC).
    :param used: the units counted.
    :param limit: the quota's limit; None without a quota, or when its limit gives None.
    :param resets: when the period ends, and the counter starts again.
    """

    subject: str
    meter: str
    period: str
    used: int
    limit: int | None
    resets: datetime

    @property
    def remaining(self) -> int | None:
        """What's left of the quota; None without one."""
        return None if self.limit is None else max(0, self.limit - self.used)


class Metering:
    """What `meter(name, amount=None)` gives the endpoint: it adds what it uses as it goes
    (`add(tokens)`), and the total is charged when it ends."""

    def __init__(self, meter: str, subject: str, remaining: int | None) -> None:
        self.meter = meter
        self.subject = subject
        #: What the quota left when the request started; None without a quota (or unread).
        self.remaining = remaining
        self.total = 0

    def add(self, amount: int) -> None:
        """Add `amount` units to what the request has used."""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("amount is a whole number of units, 0 or more")
        self.total += amount


@dataclass(slots=True)
class _Account:
    """A subject's counter of a meter in the current period, as a request found it."""

    cache: Cache
    subject: str
    meter: str
    key: str
    period: str
    resets: datetime
    expire: int
    limit: int | None


async def _not_served(exc: BaseException, connection: HTTPConnection) -> bool:
    """Whether the request that raised `exc` wasn't served (its charge is refunded): a server
    error, not a client error (4xx, a validation error) nor a client that went away once the
    endpoint ran. Leaving is the client's choice: refunding it would let a client use the
    endpoint without being charged (by closing its WebSocket, or its connection before the
    response)."""
    if isinstance(exc, WebSocketDisconnect | ClientDisconnect):
        return False
    if isinstance(exc, OSError) and isinstance(connection, Request):
        # What an ASGI 2.4 server raises from send() once the client left; the endpoint's own
        # (a database that went down) is refunded, the client still waiting for its response.
        return not await _client_gone(connection)
    if isinstance(exc, HTTPException):
        return exc.status_code >= 500
    if isinstance(exc, WebSocketException):
        return exc.code != WS_1008_POLICY_VIOLATION
    return not isinstance(exc, RequestValidationError | WebSocketRequestValidationError)


#: How many messages of a request's body `_client_gone` reads, at most, looking past them.
_MAX_UNREAD = 1024


async def _client_gone(request: Request) -> bool:
    """Whether `request`'s client went away: the server says so once the body it didn't read
    has been, without waiting for more."""
    with anyio.CancelScope() as scope:
        scope.cancel()  # a receive() that would wait returns at once, cancelled
        for _ in range(_MAX_UNREAD):
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return True
            if message["type"] != "http.request":
                break
    return False


def _subject_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | uuid.UUID):
        raise TypeError(f"the subject is an ID (a str, an int or a UUID), not {value!r}")
    text = str(value)
    if not text or len(text) > _MAX_SUBJECT or not text.isprintable():
        raise ValueError(f"the subject's ID is 1 to {_MAX_SUBJECT} printable characters")
    return text


class FastAPIAccounting:
    """Meters what each subject uses, per period, and holds it to its quotas.

    `meter(name)` is a dependency that charges each request: 1 unit by default, checked against
    the quota and counted in one atomic operation, so concurrent requests never go past it. A
    request over its quota gets a 429 with `Retry-After` (when the period ends), or a 402, and
    isn't counted. `meter(name, amount=None)` gives the endpoint a `Metering` to add what it
    uses as it goes (tokens, bytes, seconds), charged when it ends; the quota is checked when it
    starts, so the requests already running may together go past it.

    Responses carry the `RateLimit` and `RateLimit-Policy` headers of the IETF draft, the
    quota's name being the meter's, for clients to see what they have left.

    What's charged: a request that gets a client error (a 4xx, a validation error), or whose
    client went away once the endpoint ran (it closed its WebSocket, or its connection), was
    served and stays charged; one that fails on the server (an exception, a 5xx) is refunded,
    and has no record. What an endpoint added to its `Metering` is charged
    whatever happens next: it was used.

    :param subject: a dependency that returns who uses it: a user's, an API key's or a tenant's
        ID, None when nobody is authenticated (a 401, `WWW-Authenticate: <challenge>`).
    :param quotas: a `Quota` per meter at most. Meters without one are counted, not bounded.
    :param period: the period of the meters without a quota.
    :param sinks: where the usage records go; a LoggingUsageSink by default, `()` for none.
    :param cache: the Cache whose backend keeps the counters; the app's (`get_cache`) by default.
    :param fail_open: when the backend fails, let requests through uncounted (the default: the
        records still have them) or refuse them with a 503.
    :param challenge: the `WWW-Authenticate` of the 401s.
    :param headers: send the `RateLimit` and `RateLimit-Policy` headers.
    :param timeout: seconds each sink has to write a record.
    :param metrics: where to count the requests (`fastapi_alcazaba.accounting`), the units
        (`fastapi_alcazaba.accounting.usage`) and the records not written
        (`fastapi_alcazaba.accounting.undelivered`); the cache's by default.
    """

    def __init__(
        self,
        *,
        subject: Subject,
        quotas: Iterable[Quota] = (),
        period: Period = "month",
        sinks: Iterable[UsageSink] | None = None,
        cache: Cache | None = None,
        fail_open: bool = True,
        challenge: str = "Bearer",
        headers: bool = True,
        timeout: float = 5.0,
        metrics: Metrics | None = None,
    ) -> None:
        self.subject = subject
        self.quotas: dict[str, Quota] = {}
        for quota in quotas:
            if quota.meter in self.quotas:
                raise ValueError(f"the meter {quota.meter!r} has two quotas")
            self.quotas[quota.meter] = quota
        self.period = _check_period(period)
        self.sinks: list[UsageSink] = [LoggingUsageSink()] if sinks is None else list(sinks)
        if not (math.isfinite(timeout) and timeout > 0):
            raise ValueError("timeout must be a positive number of seconds")
        self.cache = cache
        self.fail_open = fail_open
        self.challenge = challenge
        self.headers = headers
        self.timeout = timeout
        self._metrics = metrics
        self._writing: set[asyncio.Future[Any]] = set()

    def meter(
        self,
        meter: str,
        *,
        amount: int | None = 1,
    ) -> Callable[..., AsyncIterator[Any]]:
        """A dependency that charges `amount` units of `meter` to the request's subject before
        the endpoint runs, within its quota, and returns the subject's ID. With `amount=None` it
        returns a `Metering`, to which the endpoint adds what it uses; the total is charged when
        it ends."""
        _check_meter(meter)
        if amount is not None and (isinstance(amount, bool) or amount < 1):
            raise ValueError("amount is a number of units, 1 or more (None: the endpoint adds)")

        if amount is None:

            async def metering(
                connection: HTTPConnection,
                response: Response,
                subject: Annotated[Any, Depends(self.subject)],
            ) -> AsyncIterator[Metering]:
                account = await self._open(connection, subject, meter)
                used = await self._take(connection, response, account, 0)
                remaining = None if used is None or account.limit is None else account.limit - used
                usage = Metering(meter, account.subject, remaining)
                try:
                    yield usage
                finally:
                    if usage.total:
                        await self._shielded(self._charge(account, usage.total, connection))

            return metering

        async def metered(
            connection: HTTPConnection,
            response: Response,
            subject: Annotated[Any, Depends(self.subject)],
        ) -> AsyncIterator[str]:
            account = await self._open(connection, subject, meter)
            counted = await self._take(connection, response, account, amount) is not None
            try:
                yield account.subject
            except GeneratorExit:
                raise  # closed without ending: charged, without a record
            except BaseException as exc:
                if await _not_served(exc, connection):
                    if counted:
                        await self._shielded(self._refund(account, amount))
                    raise
                await self._shielded(self._deliver(account, amount, connection))
                raise
            await self._shielded(self._deliver(account, amount, connection))

        return metered

    async def record(
        self,
        subject: str | int | uuid.UUID,
        meter: str,
        amount: int,
        *,
        request: HTTPConnection | None = None,
    ) -> UsageRecord:
        """Charges `amount` units of `meter` to `subject` without checking its quota (it's been
        used already): for what happens outside a metered request, such as a job or a webhook.
        Counted in the backend (when it answers) and sent to the sinks; returns the record."""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            raise ValueError("amount is a number of units, 1 or more")
        account = await self._account(self._cache_of(request), _required(subject), meter)
        return await self._shielded(self._charge(account, amount, request))

    async def usage(
        self,
        subject: str | int | uuid.UUID,
        meter: str,
        *,
        at: datetime | None = None,
        request: HTTPConnection | None = None,
    ) -> UsageReport:
        """What `subject` has used of `meter` in the period that holds `at` (now by default),
        and what its quota leaves. The counters keep the current period and the one before; the
        records have the older ones. Raises BackendUnavailable when the backend fails."""
        cache = self._cache_of(request)
        account = await self._account(cache, _required(subject), meter, at)
        period = self._period_of(meter)
        current = period_of(period, datetime.now(UTC))[1]
        previous = period_of(period, current - timedelta(microseconds=1))[1]
        if at is not None and period_of(period, at)[1] < previous:
            raise ValueError("the counters keep the current period and the one before")
        backend = cache.backend
        used = await cache.guard.run("get_counter", lambda: backend.get_counter(account.key))
        return UsageReport(
            account.subject, meter, account.period, used, account.limit, account.resets
        )

    def _period_of(self, meter: str) -> Period:
        quota = self.quotas.get(meter)
        return quota.period if quota is not None else self.period

    def _cache_of(self, connection: HTTPConnection | None) -> Cache:
        if self.cache is not None:
            return self.cache
        return get_cache(connection) if connection is not None else FastAPICache.instance()

    async def _open(self, connection: HTTPConnection, subject: Any, meter: str) -> _Account:
        subject_id = _subject_id(subject)
        if subject_id is None:
            self._metrics_of(self._cache_of(connection)).count(
                "accounting", meter=meter, outcome="unauthenticated"
            )
            if connection.scope["type"] == "websocket":
                raise WebSocketException(WS_1008_POLICY_VIOLATION, "Not authenticated")
            headers = {"WWW-Authenticate": self.challenge}
            raise HTTPException(HTTP_401_UNAUTHORIZED, "Not authenticated", headers=headers)
        return await self._account(self._cache_of(connection), subject_id, meter)

    async def _account(
        self,
        cache: Cache,
        subject: str,
        meter: str,
        at: datetime | None = None,
    ) -> _Account:
        _check_meter(meter)
        if not cache.backend.supports_counters:
            raise NotImplementedError(
                f"{type(cache.backend).__name__} can't keep counters, which accounting needs"
            )
        period = self._period_of(meter)
        now = datetime.now(UTC)
        label, _, resets = period_of(period, at if at is not None else now)
        digest = hashlib.sha256(json.dumps([meter, subject]).encode()).hexdigest()[:32]
        quota = self.quotas.get(meter)
        limit: int | None = None
        if quota is not None:
            found = quota.limit(subject) if callable(quota.limit) else quota.limit
            if isawaitable(found):
                found = await found
            if found is not None and (isinstance(found, bool) or not isinstance(found, int)):
                raise TypeError(f"the limit of {meter!r} is a number of units or None: {found!r}")
            limit = found
        # Read during the next period too; an hour more for clock differences.
        expire = math.ceil((resets - now + _LONGEST[period]).total_seconds()) + 3600
        return _Account(
            cache=cache,
            subject=subject,
            meter=meter,
            # The {hash tag} keeps a subject's periods in one Redis Cluster slot.
            key=cache.state_key("usage", f"{{{digest}}}:{label}"),
            period=label,
            resets=resets,
            expire=expire,
            limit=limit,
        )

    async def _take(
        self,
        connection: HTTPConnection,
        response: Response,
        account: _Account,
        amount: int,
    ) -> int | None:
        """Charge `amount` within the quota (only check that something is left, for 0): the
        units used then, or None when the backend failed and the request goes on uncounted."""
        cache, key, limit = account.cache, account.key, account.limit
        backend = cache.backend
        metrics = self._metrics_of(cache)
        try:
            if amount == 0:
                used = await cache.guard.run("get_counter", lambda: backend.get_counter(key))
                allowed = limit is None or used < limit
            elif limit is None:
                used = await cache.guard.run(
                    "incr", lambda: backend.incr(key, amount, account.expire)
                )
                allowed = True
            else:
                # The rate limiter's atomic check-and-add, without a previous window.
                allowed, used, _ = await cache.guard.run(
                    "incr_window",
                    lambda: backend.incr_window(
                        key, key, amount=amount, limit=limit, weight=0.0, expire=account.expire
                    ),
                )
        except BackendUnavailable as exc:
            metrics.count("accounting", meter=account.meter, outcome="unchecked")
            if isinstance(exc, CircuitOpen):
                logger.debug("Usage of %s not counted: %s", account.meter, exc)
            else:
                logger.warning("Usage of %s not counted", account.meter, exc_info=exc)
            if self.fail_open:
                return None
            if connection.scope["type"] == "websocket":
                raise WebSocketException(WS_1013_TRY_AGAIN_LATER) from exc
            raise HTTPException(HTTP_503_SERVICE_UNAVAILABLE) from exc
        headers = {}
        if self.headers and limit is not None:
            headers = self._headers(account, limit, used)
        if not allowed:
            metrics.count("accounting", meter=account.meter, outcome="exceeded")
            self._exceeded(connection, account, headers)
        metrics.count("accounting", meter=account.meter, outcome="allowed")
        for header, value in headers.items():
            response.headers.append(header, value)
        if headers:  # error responses (problem details) carry them too
            state = connection.scope.setdefault("state", {})
            state.setdefault(RATE_LIMIT_HEADERS, []).extend(headers.items())
        return used

    def _headers(self, account: _Account, limit: int, used: int) -> dict[str, str]:
        remaining = max(0, limit - used)
        reset = max(0, math.ceil((account.resets - datetime.now(UTC)).total_seconds()))
        window = _WINDOW.get(self._period_of(account.meter))
        return {
            "RateLimit": f'"{account.meter}";r={remaining};t={reset}',
            "RateLimit-Policy": f'"{account.meter}";q={limit}' + (f";w={window}" if window else ""),
        }

    def _exceeded(
        self,
        connection: HTTPConnection,
        account: _Account,
        headers: dict[str, str],
    ) -> NoReturn:
        if connection.scope["type"] == "websocket":
            raise WebSocketException(WS_1008_POLICY_VIOLATION, "Quota exceeded")
        quota = self.quotas[account.meter]
        retry = max(1, math.ceil((account.resets - datetime.now(UTC)).total_seconds()))
        raise HTTPException(
            quota.status, "Quota exceeded", headers={**headers, "Retry-After": str(retry)}
        )

    async def _refund(self, account: _Account, amount: int) -> None:
        cache = account.cache
        backend = cache.backend
        self._metrics_of(cache).count("accounting", meter=account.meter, outcome="refunded")
        try:
            await cache.guard.run(
                "incr", lambda: backend.incr(account.key, -amount, account.expire)
            )
        except BackendUnavailable:
            # Still charged in the counter, until its period ends; never in the records.
            logger.warning("Couldn't refund %s of %s", amount, account.meter, exc_info=True)

    async def _charge(
        self,
        account: _Account,
        amount: int,
        connection: HTTPConnection | None,
    ) -> UsageRecord:
        """Count `amount` without a limit (it's been used), and send its record."""
        cache = account.cache
        backend = cache.backend
        try:
            await cache.guard.run("incr", lambda: backend.incr(account.key, amount, account.expire))
        except BackendUnavailable:
            logger.warning("Usage of %s not counted", account.meter, exc_info=True)
        return await self._deliver(account, amount, connection)

    async def _deliver(
        self,
        account: _Account,
        amount: int,
        connection: HTTPConnection | None,
    ) -> UsageRecord:
        record = _record(account, amount, connection)
        metrics = self._metrics_of(account.cache)
        metrics.add("accounting.usage", amount, meter=account.meter)
        for sink in self.sinks:
            name = type(sink).__name__
            deadline = asyncio.timeout(self.timeout)
            try:
                async with deadline:
                    await sink.write(record)
            except Exception as exc:  # a sink must not keep the record from the others
                timed_out = isinstance(exc, TimeoutError) and deadline.expired()
                # The record goes in the message: logged, it isn't lost.
                logger.error(
                    "Usage record not written by %s (%s): %s",
                    name,
                    f"took longer than {self.timeout} s" if timed_out else type(exc).__name__,
                    record.to_json(),
                    exc_info=not timed_out,
                )
                reason = "timeout" if timed_out else "error"
                metrics.count("accounting.undelivered", sink=name, reason=reason)
        return record

    async def _shielded(self, work: Coroutine[Any, Any, T]) -> T:
        """`work` in a task of its own, shielded: a request cancelled meanwhile (its client
        gone, its deadline passed) still gets charged and recorded."""
        task = asyncio.ensure_future(work)
        self._writing.add(task)
        task.add_done_callback(self._writing.discard)
        return await asyncio.shield(task)

    def _metrics_of(self, cache: Cache) -> Metrics:
        return self._metrics if self._metrics is not None else cache.metrics


def _required(subject: str | int | uuid.UUID) -> str:
    subject_id = _subject_id(subject)
    if subject_id is None:
        raise ValueError("give the subject's ID")
    return subject_id


def _record(account: _Account, amount: int, connection: HTTPConnection | None) -> UsageRecord:
    method = route = None
    if connection is not None:
        scope = connection.scope
        method = scope.get("method")
        template = getattr(scope.get("route"), "path_format", None)
        if isinstance(template, str):
            route = f"{scope.get('root_path', '')}{template}"
    parent = traceparent()
    return UsageRecord(
        id=uuid.uuid4().hex,
        time=datetime.now(UTC),
        subject=account.subject,
        meter=account.meter,
        amount=amount,
        period=account.period,
        request_id=request_id(),
        trace_id=parent.split("-")[1] if parent else None,
        method=method,
        route=route,
    )
