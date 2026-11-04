"""What each user may cost the app at a provider it pays for (an LLM, SMS, OCR, maps...): a quota
per period, a rate, calls at the same time and a cap per call, for each user and for the whole
app, read from your database, so that an admin panel changes them without a deploy.

    openai = FastAPIProviderBudget(
        "openai",
        subject=current_user_id,  # your dependency: the user's ID, None without one
        limits=limits_of,  # async (user_id) -> BudgetLimits | None, from your database
        total=BudgetLimits(quota=50_000_000),  # the whole app's, a month
    )

    @app.post("/summaries")
    async def summarize(
        text: Text,
        call: Annotated[ProviderCall, Depends(openai.reserve(2_000, at_least=100))],
    ) -> Summary:
        result = await llm.complete(text, max_tokens=call.reserved)
        call.use(result.usage.total_tokens)
        return result.summary

Each call reserves its units before it's made, from the user's quota and from the app's, in
atomic operations, and gives back what it didn't use when it ends: calls made at the same time
never go past either. `SQLBudgetLimits` keeps the limits in a table of yours.
"""

import asyncio
import hashlib
import json
import logging
import math
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from inspect import isawaitable
from typing import Annotated, Any, TypeVar

from fastapi import Depends
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.status import (
    HTTP_401_UNAUTHORIZED,
    HTTP_402_PAYMENT_REQUIRED,
    HTTP_403_FORBIDDEN,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from fastapi_alcazaba.accounting.meter import (
    PERIODS,
    Period,
    _subject_id,
    period_of,
)
from fastapi_alcazaba.accounting.records import LoggingUsageSink, UsageRecord, UsageSink
from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.asgi import RATE_LIMIT_HEADERS
from fastapi_alcazaba.core.errors import BackendUnavailable, CircuitOpen
from fastapi_alcazaba.observability.request_id import request_id
from fastapi_alcazaba.rate_limit.concurrency import (
    ConcurrencyLimitExceeded,
    FastAPIConcurrencyLimiter,
)
from fastapi_alcazaba.response_cache.caches import Cache, get_cache, resolve_cache
from fastapi_alcazaba.telemetry.metrics import Metrics
from fastapi_alcazaba.telemetry.tracing import traceparent

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

T = TypeVar("T")

_LONGEST = {"hour": timedelta(hours=1), "day": timedelta(days=1), "month": timedelta(days=31)}
#: Tries at reserving what's left, when `at_least` lets a call take less than it asked for.
_TRIES = 3


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """What a user (or the whole app, as `total`) may spend at a provider. Read from your
    database, so that an admin panel can change them.

    :param quota: units per `period` (tokens, messages, pages, cents...); None for no limit.
    :param period: the calendar hour, day or month (UTC) the quota is for.
    :param rate: calls per `rate_window` seconds, in a sliding window; None for no limit.
    :param rate_window: the rate's window, in seconds.
    :param concurrency: calls at the same time; None for no limit.
    :param max_per_call: the most units one call may reserve; None for no cap.
    :param enabled: False refuses every call (a 403): a user cut off, or a provider turned off.
    """

    quota: int | None = None
    period: Period = "month"
    rate: int | None = None
    rate_window: int = 60
    concurrency: int | None = None
    max_per_call: int | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        for name in ("quota", "rate", "concurrency", "max_per_call"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise TypeError(f"{name} is a whole number, or None: {value!r}")
        if self.quota is not None and self.quota < 0:
            raise ValueError("quota can't be negative")
        if self.period not in PERIODS:
            raise ValueError(f"period is one of {', '.join(PERIODS)}")
        for name in ("rate", "concurrency", "max_per_call"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} is at least 1, or None")
        if isinstance(self.rate_window, bool) or not 1 <= self.rate_window <= 86_400:
            raise ValueError("rate_window is 1 to 86400 seconds")


#: Where the limits come from: the same for everybody, or a function (plain or async) of the
#: user's ID that reads them; None from it gives the budget's `default`.
LimitsSource = BudgetLimits | Callable[[str], BudgetLimits | None | Awaitable[BudgetLimits | None]]
#: The whole app's: fixed, or a function (plain or async) without arguments; None for no limit.
TotalSource = BudgetLimits | Callable[[], BudgetLimits | None | Awaitable[BudgetLimits | None]]


class BudgetRefused(HTTPException):
    """A call the budget refused, and why (`reason`): `disabled` (403), `quota` or `total` (the
    user's or the app's quota is spent: `exceeded_status`, 402 by default), `rate` or
    `concurrency` (429), `unavailable` (503: the backend or the limits couldn't be read).
    `Retry-After` says when to try again, when that's known."""

    def __init__(
        self,
        status_code: int,
        reason: str,
        detail: str,
        retry_after: int | None = None,
    ) -> None:
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        super().__init__(status_code, detail, headers=headers)
        self.reason = reason


class ProviderCall:
    """A call to the provider, with the units reserved for it: tell the provider not to use more
    (`max_tokens=call.reserved`), and say what it used (`use`). What isn't used is given back
    when the call ends; when it fails before using anything (an exception, a 5xx), all of it."""

    def __init__(self, provider: str, subject: str, reserved: int) -> None:
        self.provider = provider
        self.subject = subject
        #: The units reserved for this call.
        self.reserved = reserved
        #: The units used, as `use` added them.
        self.used = 0

    @property
    def remaining(self) -> int:
        """Reserved and not used yet (never below 0)."""
        return max(0, self.reserved - self.used)

    def use(self, units: int) -> None:
        """Add `units` to what the call used. More than reserved is charged too (the provider
        billed it) and logged: cap the call with `reserved`."""
        if isinstance(units, bool) or not isinstance(units, int) or units < 0:
            raise ValueError("units is a whole number, 0 or more")
        self.used += units


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """What a user (`subject`, None for the whole app) has spent at a provider this period."""

    provider: str
    subject: str | None
    used: int
    limit: int | None
    period: str
    resets: datetime

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)


@dataclass(slots=True)
class _Counter:
    """A quota's counter in the current period: the user's or the app's."""

    key: str
    label: str
    resets: datetime
    expire: int
    limit: int | None
    reserved: int = 0
    counted: bool = False  # whether `reserved` reached the backend
    used: int = 0  # the counter once reserved, as the backend answered


class FastAPIProviderBudget:
    """Holds what each user, and the whole app, spend at a provider to their limits.

    `reserve(units)` is a dependency that, before the endpoint runs, checks that the user (and
    the app) may call the provider (`enabled`), within its rate, with a free slot of its
    concurrency, and reserves `units` from the user's quota and from the app's, each in one
    atomic operation; it gives the endpoint a `ProviderCall`. When the endpoint ends, what it
    didn't `use` goes back to both quotas, and what it used gets a usage record (the `meter` is
    the provider). `call()` does the same outside requests (a task, a WebSocket).

    The limits are read through `limits` (per user) and `total` (the app's), kept in each
    process for `settings_ttl` seconds: an admin panel that changes them in the database is
    obeyed by every process within that time, at once by the one that calls `invalidate()`.
    While the database can't be read, the limits read last are kept; without them, calls get a
    503.

    :param provider: its name, in the counters' keys, the records and the metrics: "openai".
    :param subject: a dependency that returns the user's ID (a str, an int or a UUID), None when
        nobody is authenticated (a 401).
    :param limits: each user's `BudgetLimits`: fixed, or a function of the user's ID (plain or
        async) that reads them; None from it, `default`.
    :param total: the whole app's: a `BudgetLimits` (its quota, rate and concurrency count every
        user's calls together), a function without arguments that reads it, or None.
    :param default: the limits of the users `limits` gives None for: no limit by default.
    :param settings_ttl: seconds the limits read are kept in each process.
    :param cache_size: users whose limits are kept per process (the least used are dropped).
    :param exceeded_status: the status when a quota is spent: 402 (a plan to upgrade), or 429.
    :param wait: seconds to wait for a slot of `concurrency` before a 429.
    :param fail_open: when the backend fails, make the calls uncounted (the records still have
        them) instead of refusing them with a 503: off by default, as going past a provider's
        budget costs money.
    :param sinks: where the usage records go; a LoggingUsageSink by default, `()` for none.
    :param cache: the Cache whose backend keeps the counters; the app's (`get_cache`) by default.
    :param headers: send the `RateLimit` and `RateLimit-Policy` headers of the user's quota.
    :param timeout: seconds each sink has to write a record.
    :param metrics: where to count the calls (`fastapi_alcazaba.budget`) and the units
        (`fastapi_alcazaba.budget.usage`); the cache's by default.
    """

    def __init__(
        self,
        provider: str,
        *,
        subject: Callable[..., Any],
        limits: LimitsSource,
        total: TotalSource | None = None,
        default: BudgetLimits | None = None,
        settings_ttl: float = 30.0,
        cache_size: int = 10_000,
        exceeded_status: int = HTTP_402_PAYMENT_REQUIRED,
        wait: float = 0.0,
        fail_open: bool = False,
        sinks: Iterable[UsageSink] | None = None,
        cache: Cache | None = None,
        headers: bool = True,
        timeout: float = 5.0,
        metrics: Metrics | None = None,
    ) -> None:
        if not provider or len(provider) > 64 or not provider.isprintable():
            raise ValueError("provider is a name of 1 to 64 printable characters")
        if not (math.isfinite(settings_ttl) and settings_ttl >= 0):
            raise ValueError("settings_ttl is a number of seconds, 0 or more")
        if cache_size < 1:
            raise ValueError("cache_size must be at least 1")
        if exceeded_status not in (HTTP_402_PAYMENT_REQUIRED, HTTP_429_TOO_MANY_REQUESTS):
            raise ValueError("exceeded_status is 402 or 429")
        if not (math.isfinite(wait) and wait >= 0):
            raise ValueError("wait is a number of seconds, 0 or more")
        if not (math.isfinite(timeout) and timeout > 0):
            raise ValueError("timeout must be a positive number of seconds")
        self.provider = provider
        self.subject = subject
        self.limits = limits
        self.total = total
        self.default = default if default is not None else BudgetLimits()
        self.settings_ttl = settings_ttl
        self.cache_size = cache_size
        self.exceeded_status = exceeded_status
        self.wait = wait
        self.fail_open = fail_open
        self.sinks: list[UsageSink] = [LoggingUsageSink()] if sinks is None else list(sinks)
        self.cache = cache
        self.headers = headers
        self.timeout = timeout
        self._metrics = metrics
        # subject (None: the app's) -> (read at, limits); the least recently used dropped first.
        self._read: OrderedDict[str | None, tuple[float, BudgetLimits | None]] = OrderedDict()
        self._limiters: dict[tuple[int, int], FastAPIConcurrencyLimiter] = {}
        self._writing: set[asyncio.Future[Any]] = set()

    # region The dependency, and calls outside requests

    def reserve(
        self,
        units: int = 1,
        *,
        at_least: int | None = None,
    ) -> Callable[..., AsyncIterator[ProviderCall]]:
        """A dependency that reserves `units` for a call of the request's user, and gives the
        endpoint its `ProviderCall`. With `at_least`, a user (or an app) with less left gets what
        is left, if it's `at_least` or more: for calls that can be made smaller (`max_tokens`).
        `max_per_call` caps `units` too."""
        _check_units(units, at_least)

        async def reserved(
            connection: HTTPConnection,
            response: Response,
            subject: Annotated[Any, Depends(self.subject)],
        ) -> AsyncIterator[ProviderCall]:
            subject_id = _subject_id(subject)
            if subject_id is None:
                self._count(self._cache_of(connection), "unauthenticated")
                raise HTTPException(HTTP_401_UNAUTHORIZED, "Not authenticated")
            async with self._call(
                subject_id, units, at_least, self._cache_of(connection), connection, response
            ) as call:
                yield call

        return reserved

    @asynccontextmanager
    async def call(
        self,
        subject: str | int | uuid.UUID,
        units: int = 1,
        *,
        at_least: int | None = None,
        cache: Cache | None = None,
    ) -> AsyncIterator[ProviderCall]:
        """`reserve` outside requests (a task, a WebSocket message):

            async with openai.call(user_id, 2_000, at_least=100) as call:
                result = await llm.complete(text, max_tokens=call.reserved)
                call.use(result.usage.total_tokens)

        Raises BudgetRefused when the call can't be made."""
        _check_units(units, at_least)
        subject_id = _subject_id(subject)
        if subject_id is None:
            raise ValueError("give the user's ID")
        found = cache if cache is not None else resolve_cache(self.cache)
        async with self._call(subject_id, units, at_least, found, None, None) as made:
            yield made

    # endregion

    # region Reading and resetting

    async def usage(
        self,
        subject: str | int | uuid.UUID | None,
        *,
        cache: Cache | None = None,
    ) -> BudgetUsage:
        """What `subject` (None: the whole app) has spent this period, what its quota is and
        when it starts again: for the user's page, or the admin panel. Raises
        BackendUnavailable when the backend fails."""
        subject_id = None if subject is None else _subject_id(subject)
        found = cache if cache is not None else resolve_cache(self.cache)
        limits = await self._limits_of(subject_id)
        if limits is None:
            limits = BudgetLimits()
        counter = self._counter(found, subject_id, limits)
        backend = shared_backend(found.backend)
        used = await found.guard.run("get_counter", lambda: backend.get_counter(counter.key))
        return BudgetUsage(
            self.provider, subject_id, used, counter.limit, counter.label, counter.resets
        )

    async def reset(
        self,
        subject: str | int | uuid.UUID | None,
        *,
        cache: Cache | None = None,
    ) -> None:
        """Forget what `subject` (None: the whole app) spent this period: an admin giving a user
        their quota back. Raises BackendUnavailable when the backend fails."""
        subject_id = None if subject is None else _subject_id(subject)
        found = cache if cache is not None else resolve_cache(self.cache)
        limits = await self._limits_of(subject_id) or BudgetLimits()
        key = self._counter(found, subject_id, limits).key
        backend = shared_backend(found.backend)
        await found.guard.run("delete", lambda: backend.clear(key=key))
        logger.info("Budget of %s at %s reset", subject_id or "the app", self.provider)

    def invalidate(
        self,
        subject: str | int | uuid.UUID | None = None,
        *,
        total: bool = False,
    ) -> None:
        """Read `subject`'s limits again on their next call (every user's without one), and the
        app's with `total`: after the admin panel changed them. Only this process: the others
        read them again within `settings_ttl`."""
        if subject is None and not total:
            self._read.clear()
            return
        if subject is not None:
            self._read.pop(_subject_id(subject), None)
        if total:
            self._read.pop(None, None)

    async def aclose(self) -> None:
        """Wait for the records still being written (at shutdown)."""
        if self._writing:
            await asyncio.gather(*self._writing, return_exceptions=True)

    # endregion

    # region Limits

    async def _limits_of(self, subject: str | None) -> BudgetLimits | None:
        """The limits of `subject`, or the app's (None): as kept, or read again when older than
        `settings_ttl`; the old ones while they can't be read."""
        now = time.monotonic()
        kept = self._read.get(subject)
        if kept is not None and now - kept[0] < self.settings_ttl:
            self._read.move_to_end(subject)
            return kept[1]
        source: Any = self.limits if subject is not None else self.total
        try:
            if source is None or isinstance(source, BudgetLimits):
                found = source
            else:
                found = source(subject) if subject is not None else source()
                if isawaitable(found):
                    found = await found
            if found is not None and not isinstance(found, BudgetLimits):
                raise TypeError(f"limits are BudgetLimits or None, not {type(found).__name__}")
        except Exception as exc:
            if kept is not None:
                logger.warning(
                    "The limits of %s at %s weren't read: the last ones are kept",
                    subject or "the app",
                    self.provider,
                    exc_info=exc,
                )
                return kept[1]
            logger.error(
                "The limits of %s at %s weren't read",
                subject or "the app",
                self.provider,
                exc_info=exc,
            )
            raise BudgetRefused(
                HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "Budget unavailable", 5
            ) from exc
        if subject is not None and found is None:
            found = self.default
        self._read[subject] = (now, found)
        self._read.move_to_end(subject)
        while len(self._read) > self.cache_size:
            self._read.popitem(last=False)
        return found

    # endregion

    # region The call

    @asynccontextmanager
    async def _call(
        self,
        subject: str,
        units: int,
        at_least: int | None,
        cache: Cache,
        connection: HTTPConnection | None,
        response: Response | None,
    ) -> AsyncIterator[ProviderCall]:
        limits = await self._limits_of(subject)
        assert limits is not None  # noqa: S101  # a user's are never None (the default)
        total = await self._limits_of(None)
        if not limits.enabled or (total is not None and not total.enabled):
            self._count(cache, "disabled")
            raise BudgetRefused(HTTP_403_FORBIDDEN, "disabled", "Provider not available")
        async with AsyncExitStack() as stack:
            for scoped, limit in ((subject, limits), (None, total)):
                if limit is not None and limit.rate is not None:
                    await self._rate(cache, scoped, limit)
            for scoped, limit in ((subject, limits), (None, total)):
                if limit is not None and limit.concurrency is not None:
                    await stack.enter_async_context(self._slot(cache, scoped, limit.concurrency))
            mine = self._counter(cache, subject, limits)
            app = self._counter(cache, None, total) if total is not None else None
            wanted = min(units, limits.max_per_call or units)
            if total is not None and total.max_per_call is not None:
                wanted = min(wanted, total.max_per_call)
            floor = None if at_least is None else min(at_least, wanted)
            reserved = await self._take(cache, mine, wanted, floor, "quota")
            if app is not None:
                try:
                    reserved = await self._take(cache, app, reserved, floor, "total")
                except BaseException:
                    await self._shielded(self._give_back(cache, mine, mine.reserved))
                    raise
                if reserved < mine.reserved:  # the app had less left than the user
                    await self._shielded(self._give_back(cache, mine, mine.reserved - reserved))
            if response is not None and connection is not None:
                self._headers(connection, response, mine)
            self._count(cache, "reserved")
            made = ProviderCall(self.provider, subject, reserved)
            try:
                yield made
            finally:
                # Whatever happened, only what the call used stays spent: nothing when it failed
                # before the provider was called.
                await self._shielded(self._settle(cache, made, [mine, app], connection))

    async def _take(
        self,
        cache: Cache,
        counter: _Counter,
        units: int,
        at_least: int | None,
        reason: str,
    ) -> int:
        """Reserve `units` (or what's left, down to `at_least`) on `counter`: what was reserved.
        Raises BudgetRefused when there isn't enough."""
        backend = shared_backend(cache.backend)
        key, limit = counter.key, counter.limit
        wanted = units
        try:
            for _ in range(_TRIES):
                if limit is None:
                    used = await cache.guard.run(
                        "incr", partial(backend.incr, key, wanted, counter.expire)
                    )
                    added = True
                else:
                    added, used, _ = await cache.guard.run(
                        "incr_window",
                        # The rate limiter's atomic check-and-add, without a previous window.
                        partial(
                            backend.incr_window,
                            key,
                            key,
                            amount=wanted,
                            limit=limit,
                            weight=0.0,
                            expire=counter.expire,
                        ),
                    )
                if added:
                    counter.reserved, counter.counted = wanted, True
                    counter.used = used if limit is not None else 0
                    return wanted
                left = max(0, limit - used)  # type: ignore[operator]  # a limit, then
                if at_least is None or left < max(at_least, 1):
                    break
                wanted = min(units, left)  # what's left, unless others take it meanwhile
        except BackendUnavailable as exc:
            self._count(cache, "unchecked")
            if isinstance(exc, CircuitOpen):
                logger.debug("Budget of %s not checked: %s", self.provider, exc)
            else:
                logger.warning("Budget of %s not checked", self.provider, exc_info=exc)
            if not self.fail_open:
                raise BudgetRefused(
                    HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "Budget unavailable", 5
                ) from exc
            counter.reserved, counter.counted = units, False
            return units
        self._count(cache, "exceeded" if reason == "quota" else "total_exceeded")
        retry_after = max(1, math.ceil((counter.resets - datetime.now(UTC)).total_seconds()))
        detail = "Quota exceeded" if reason == "quota" else "Provider budget exceeded"
        raise BudgetRefused(self.exceeded_status, reason, detail, retry_after)

    async def _settle(
        self,
        cache: Cache,
        made: ProviderCall,
        counters: list[_Counter | None],
        connection: HTTPConnection | None,
    ) -> None:
        """Give back what the call didn't use (or charge what it used past its reservation),
        and send the record of what it used."""
        if made.used > made.reserved:
            logger.warning(
                "A call to %s used %d units, %d reserved: charged",
                self.provider,
                made.used,
                made.reserved,
            )
        for counter in counters:
            if counter is not None and counter.counted:
                await self._give_back(cache, counter, counter.reserved - made.used)
        if not made.used:
            self._count(cache, "unused")
        if made.used:
            mine = counters[0]
            assert mine is not None  # noqa: S101  # the user's counter is always there
            await self._deliver(cache, made, mine.label, connection)

    async def _give_back(self, cache: Cache, counter: _Counter, units: int) -> None:
        """Add `-units` to the counter: give back what wasn't used (or, negative, charge more)."""
        if not units:
            return
        backend = shared_backend(cache.backend)
        try:
            await cache.guard.run("incr", lambda: backend.incr(counter.key, -units, counter.expire))
        except BackendUnavailable:
            logger.warning(
                "Couldn't give back %d units of %s: they stay spent",
                units,
                self.provider,
                exc_info=True,
            )
            return
        counter.reserved -= units
        counter.used -= units

    async def _rate(self, cache: Cache, subject: str | None, limits: BudgetLimits) -> None:
        """Count a call in `limits.rate` per `rate_window` (a sliding window, as the rate
        limiter's); a 429 when there's no room."""
        assert limits.rate is not None  # noqa: S101  # checked by the caller
        backend = shared_backend(cache.backend)
        window = limits.rate_window * 1000
        now = time.time() * 1000
        index, elapsed = divmod(now, window)
        base = cache.state_key("budget-rate", f"{{{self._digest(subject)}}}:{limits.rate_window}")
        try:
            added, current, previous = await cache.guard.run(
                "incr_window",
                lambda: backend.incr_window(
                    f"{base}:{int(index)}",
                    f"{base}:{int(index) - 1}",
                    amount=1,
                    limit=limits.rate,  # type: ignore[arg-type]  # checked above
                    weight=(window - elapsed) / window,
                    expire=2 * limits.rate_window + 1,
                ),
            )
        except BackendUnavailable as exc:
            self._count(cache, "unchecked")
            if not self.fail_open:
                raise BudgetRefused(
                    HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "Budget unavailable", 5
                ) from exc
            return
        if not added:
            self._count(cache, "rate_limited")
            # Room comes back as the previous window weighs less: by the end of this one at most.
            del current, previous
            retry_after = max(1, math.ceil((window - elapsed) / 1000))
            raise BudgetRefused(HTTP_429_TOO_MANY_REQUESTS, "rate", "Too many calls", retry_after)

    @asynccontextmanager
    async def _slot(self, cache: Cache, subject: str | None, limit: int) -> AsyncIterator[None]:
        """A slot of `limit` calls at the same time, of `subject` (None: the whole app's)."""
        limiter = self._limiters.get((id(cache), limit))
        if limiter is None:
            limiter = FastAPIConcurrencyLimiter(
                limit,
                name=f"budget:{self.provider}",
                wait=self.wait,
                cache=cache,
                fail_open=self.fail_open,
            )
            self._limiters[id(cache), limit] = limiter
        try:
            async with limiter.slot(key=self._digest(subject)):
                yield
        except ConcurrencyLimitExceeded:
            self._count(cache, "concurrency")
            raise BudgetRefused(
                HTTP_429_TOO_MANY_REQUESTS, "concurrency", "Too many calls at once", 1
            ) from None
        except BackendUnavailable as exc:
            self._count(cache, "unchecked")
            raise BudgetRefused(
                HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "Budget unavailable", 5
            ) from exc

    # endregion

    # region Helpers

    def _digest(self, subject: str | None) -> str:
        """The counters' name for `subject` (None: the app), which never reaches the backend."""
        return hashlib.sha256(json.dumps([self.provider, subject]).encode()).hexdigest()[:32]

    def _counter(self, cache: Cache, subject: str | None, limits: BudgetLimits) -> _Counter:
        now = datetime.now(UTC)
        label, _, resets = period_of(limits.period, now)
        # Read during the next period too; an hour more for clock differences.
        expire = math.ceil((resets - now + _LONGEST[limits.period]).total_seconds()) + 3600
        # The {hash tag} keeps a subject's periods in one Redis Cluster slot.
        key = cache.state_key("budget", f"{{{self._digest(subject)}}}:{label}")
        return _Counter(key, label, resets, expire, limits.quota)

    def _headers(self, connection: HTTPConnection, response: Response, mine: _Counter) -> None:
        if not self.headers or mine.limit is None or not mine.counted:
            return
        # What's left once this call's reservation is taken: what other calls may use now.
        reset = max(0, math.ceil((mine.resets - datetime.now(UTC)).total_seconds()))
        remaining = max(0, mine.limit - mine.used)
        headers = {
            "RateLimit": f'"{self.provider}";r={remaining};t={reset}',
            "RateLimit-Policy": f'"{self.provider}";q={mine.limit}',
        }
        for name, value in headers.items():
            response.headers.append(name, value)
        state = connection.scope.setdefault("state", {})
        state.setdefault(RATE_LIMIT_HEADERS, []).extend(headers.items())

    async def _deliver(
        self,
        cache: Cache,
        made: ProviderCall,
        period: str,
        connection: HTTPConnection | None,
    ) -> None:
        method = route = None
        if connection is not None:
            method = connection.scope.get("method")
            template = getattr(connection.scope.get("route"), "path_format", None)
            if isinstance(template, str):
                route = f"{connection.scope.get('root_path', '')}{template}"
        parent = traceparent()
        record = UsageRecord(
            id=uuid.uuid4().hex,
            time=datetime.now(UTC),
            subject=made.subject,
            meter=self.provider,
            amount=made.used,
            period=period,
            request_id=request_id(),
            trace_id=parent.split("-")[1] if parent else None,
            method=method,
            route=route,
        )
        metrics = self._metrics_of(cache)
        metrics.add("budget.usage", made.used, provider=self.provider)
        for sink in self.sinks:
            deadline = asyncio.timeout(self.timeout)
            try:
                async with deadline:
                    await sink.write(record)
            except Exception as exc:  # a sink must not keep the record from the others
                timed_out = isinstance(exc, TimeoutError) and deadline.expired()
                logger.error(
                    "Usage record not written by %s (%s): %s",
                    type(sink).__name__,
                    f"took longer than {self.timeout} s" if timed_out else type(exc).__name__,
                    record.to_json(),
                    exc_info=not timed_out,
                )

    async def _shielded(self, work: Coroutine[Any, Any, T]) -> T:
        """`work` in a task of its own, shielded: a request cancelled meanwhile still gives back
        what it didn't use, and gets its record."""
        task = asyncio.ensure_future(work)
        self._writing.add(task)
        task.add_done_callback(self._writing.discard)
        return await asyncio.shield(task)

    def _cache_of(self, connection: HTTPConnection) -> Cache:
        return self.cache if self.cache is not None else get_cache(connection)

    def _metrics_of(self, cache: Cache) -> Metrics:
        return self._metrics if self._metrics is not None else cache.metrics

    def _count(self, cache: Cache, outcome: str) -> None:
        self._metrics_of(cache).count("budget", provider=self.provider, outcome=outcome)

    # endregion


def _check_units(units: int, at_least: int | None) -> None:
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        raise ValueError("units is a whole number, 1 or more")
    if at_least is not None and (
        isinstance(at_least, bool) or not isinstance(at_least, int) or not 1 <= at_least <= units
    ):
        raise ValueError("at_least is 1 to units, or None")
