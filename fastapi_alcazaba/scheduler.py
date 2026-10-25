"""Jobs that run on a schedule, every N seconds or by a cron expression, once per tick across every
process and container that shares the cache's backend.

    scheduler = FastAPIScheduler()

    @scheduler.every(minutes=5)
    async def refresh_catalog() -> None: ...

    @scheduler.cron("0 3 * * *", tz="Europe/Madrid")
    async def nightly_report() -> None: ...

    app = FastAPI(lifespan=scheduler.lifespan)

Each process runs the same schedule. At each tick, the processes claim it in the backend (an
atomic counter named after the job and the tick) and only the first runs it; a lock held while the
job runs keeps a run that outlasts its interval from overlapping the next one elsewhere.
`exclusive=False` runs a job in every process instead (to refresh something kept in memory).
"""

import asyncio
import hashlib
import inspect
import logging
import math
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as clock_time
from types import TracebackType
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from fastapi_alcazaba.cache import Cache
from fastapi_alcazaba.errors import BackendUnavailable
from fastapi_alcazaba.locks import Lock
from fastapi_alcazaba.metrics import Metrics

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

F = TypeVar("F", bound=Callable[..., Any])

# Seconds the lock of a running job outlives a process that dies while running it; renewed while
# the job runs.
_RUN_LOCK_TTL = 60.0
# Whatever an expression asks for comes back within 8 years (29 February, but not in 2100).
_MAX_DAYS = 8 * 366

_MONTHS = {
    name: number
    for number, name in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)
}
_WEEKDAYS = {name: number for number, name in enumerate("sun mon tue wed thu fri sat".split())}
_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def _value(text: str, low: int, high: int, names: dict[str, int]) -> int:
    if text.lower() in names:
        return names[text.lower()]
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"{text!r} isn't a number")
    number = int(text)
    if not low <= number <= high:
        raise ValueError(f"{number} isn't between {low} and {high}")
    return number


def _field(text: str, low: int, high: int, names: dict[str, int]) -> frozenset[int]:
    """The values of one field: `*`, `5`, `1-5`, `*/15`, `10-50/10`, `mon-fri` and lists of them."""
    values: set[int] = set()
    for item in text.split(","):
        part, slash, step_text = item.partition("/")
        if slash and not (step_text.isascii() and step_text.isdigit() and int(step_text) > 0):
            raise ValueError(f"{step_text!r} isn't a step")
        step = int(step_text) if slash else 1
        if part == "*":
            start, end = low, high
        else:
            first, dash, last = part.partition("-")
            start = _value(first, low, high, names)
            end = _value(last, low, high, names) if dash else (high if slash else start)
            if start > end:
                raise ValueError(f"{part!r} goes backwards")
        values.update(range(start, end + 1, step))
    return frozenset(values)


class _Cron:
    """A cron expression (minute, hour, day of the month, month, day of the week) in a time zone.

    When the clocks change, it does what cronie does. A job at a fixed time (no `*` in its minute
    or hour) follows the wall clock: in an hour that is skipped it runs an hour later, in one that
    repeats it runs once. A job with `*` there follows real time: every 30 minutes stays every 30
    minutes, through both.
    """

    def __init__(self, expression: str, tz: tzinfo) -> None:
        fields = _MACROS.get(expression.strip().lower(), expression).split()
        try:
            if len(fields) != 5:
                raise ValueError("it needs 5 fields: minute, hour, day, month, day of the week")
            minute, hour, day, month, weekday = fields
            self.minutes = sorted(_field(minute, 0, 59, {}))
            self.hours = sorted(_field(hour, 0, 23, {}))
            self.days = _field(day, 1, 31, {})
            self.months = _field(month, 1, 12, _MONTHS)
            self.weekdays = frozenset(value % 7 for value in _field(weekday, 0, 7, _WEEKDAYS))
        except ValueError as exc:
            raise ValueError(f"invalid cron expression {expression!r}: {exc}") from None
        # Both days restricted: either matches (as in every cron). One of them `*`: the other.
        self.any_day = day.startswith("*") or weekday.startswith("*")
        self.wall_clock = not (minute.startswith("*") or hour.startswith("*"))
        self.expression = expression
        self.tz = tz
        self.next_after(datetime(2000, 1, 1, tzinfo=UTC))  # one that never matches fails now

    def _matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        in_month, on_weekday = day.day in self.days, day.isoweekday() % 7 in self.weekdays
        return in_month and on_weekday if self.any_day else in_month or on_weekday

    def _instants(self, day: date) -> Iterator[datetime]:
        for hour in self.hours:
            for minute in self.minutes:
                local = datetime.combine(day, clock_time(hour, minute))
                if self.wall_clock:
                    # fold=0: the first of two, and a time that doesn't exist an hour later.
                    yield local.replace(tzinfo=self.tz).astimezone(UTC)
                    continue
                for fold in (0, 1):
                    instant = local.replace(tzinfo=self.tz, fold=fold).astimezone(UTC)
                    if instant.astimezone(self.tz).replace(tzinfo=None) == local:  # it exists
                        yield instant

    def next_after(self, after: datetime) -> datetime:
        """The first time after `after`, in UTC."""
        after = after.astimezone(UTC)
        first_day = after.astimezone(self.tz).date()
        for offset in range(_MAX_DAYS):
            day = first_day + timedelta(days=offset)
            if self._matches(day):
                # The whole day, then the earliest: in a repeated hour, local times aren't in order.
                found = [instant for instant in self._instants(day) if instant > after]
                if found:
                    return min(found)
        raise ValueError(f"invalid cron expression {self.expression!r}: it never matches")


class _Every:
    """Every `seconds`, at multiples of it since the Unix epoch: every 5 minutes is at :00, :05...
    (UTC), the same ticks for every process."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def next_after(self, after: datetime) -> datetime:
        tick = math.floor(after.timestamp() / self.seconds) + 1
        return datetime.fromtimestamp(tick * self.seconds, UTC)


@dataclass(eq=False)
class _Job:
    name: str
    func: Callable[[], Any]
    trigger: _Cron | _Every
    exclusive: bool
    next_run: datetime | None = None
    running: bool = False
    task: "asyncio.Task[None] | None" = None


def _is_async(func: Callable[..., Any]) -> bool:
    # A function, or an object with an async __call__.
    return inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(type(func).__call__)


class FastAPIScheduler:
    """Runs jobs on a schedule in each process, each tick once across them all.

    Start it with the app: `FastAPI(lifespan=scheduler.lifespan)`, or `async with scheduler:`
    inside your own lifespan (after `FastAPICache.init`).

    :param cache: where the ticks are claimed; FastAPICache's by default, resolved at `start()`.
        Not needed when every job has `exclusive=False`.
    :param fail_open: when the backend fails, run the exclusive jobs anyway, maybe in every
        process, instead of skipping the tick (the default).
    :param grace: seconds `stop()` waits for the jobs that are running before cancelling them.
    """

    def __init__(
        self, cache: Cache | None = None, *, fail_open: bool = False, grace: float = 10.0
    ) -> None:
        if grace < 0:
            raise ValueError("grace can't be negative")
        self.cache = cache
        self.fail_open = fail_open
        self.grace = grace
        self._jobs: dict[str, _Job] = {}
        self._resolved: Cache | None = None
        self._started = False
        self._stopping = False

    def every(
        self,
        *,
        seconds: float = 0,
        minutes: float = 0,
        hours: float = 0,
        days: float = 0,
        name: str | None = None,
        exclusive: bool = True,
    ) -> Callable[[F], F]:
        """Run the decorated function (async, or sync in a thread; no arguments) every interval,
        at multiples of it since the Unix epoch: `every(hours=1)` runs on the hour (UTC). For
        other times, use `cron`.

        :param name: the job's name in the backend, logs and metrics; the function's module and
            name by default. Two jobs can't have the same.
        :param exclusive: run each tick in one process only.
        """
        interval = seconds + 60 * minutes + 3600 * hours + 86400 * days
        if interval <= 0:
            raise ValueError("the interval must be positive")
        return self._register(_Every(interval), name, exclusive)

    def cron(
        self,
        expression: str,
        *,
        tz: str | tzinfo = "UTC",
        name: str | None = None,
        exclusive: bool = True,
    ) -> Callable[[F], F]:
        """Run the decorated function when the cron `expression` says, in the time zone `tz`:
        five fields (minute, hour, day of the month, month, day of the week, Sunday being 0 or 7)
        with `*`, lists, ranges, steps and names (`*/15 9-17 * * mon-fri`), or `@hourly`,
        `@daily`, `@weekly`, `@monthly`, `@yearly`."""
        zone = ZoneInfo(tz) if isinstance(tz, str) else tz
        return self._register(_Cron(expression, zone), name, exclusive)

    def _register(
        self, trigger: _Cron | _Every, name: str | None, exclusive: bool
    ) -> Callable[[F], F]:
        def register(func: F) -> F:
            job_name = name if name is not None else f"{func.__module__}.{func.__qualname__}"
            if job_name in self._jobs:
                raise ValueError(f"there's already a job named {job_name!r}")
            if self._started:
                raise RuntimeError("add jobs before the scheduler starts")
            try:
                inspect.signature(func).bind()
            except TypeError:
                raise TypeError(f"{job_name} must take no arguments") from None
            self._jobs[job_name] = _Job(job_name, func, trigger, exclusive)
            return func

        return register

    def next_run(self, name: str) -> datetime | None:
        """When the job `name` runs next (UTC), None while the scheduler isn't running."""
        return self._jobs[name].next_run

    async def start(self) -> None:
        """Start running the jobs."""
        if self._started:
            raise RuntimeError("the scheduler is already running")
        if any(job.exclusive for job in self._jobs.values()):
            self._resolved = self._resolve()
            backend = self._resolved.backend
            if not backend.supports_counters:
                raise NotImplementedError(
                    f"{type(backend).__name__} can't keep counters, which exclusive jobs need"
                )
        self._started, self._stopping = True, False
        loop = asyncio.get_running_loop()
        for job in self._jobs.values():
            job.task = loop.create_task(self._loop(job), name=f"scheduled job {job.name}")

    async def stop(self) -> None:
        """Stop: jobs waiting for their next run are cancelled at once, those running get
        `grace` seconds to finish (a sync job's thread can't be stopped: it runs to its end)."""
        if not self._started:
            return
        self._stopping = True
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        for job in self._jobs.values():
            if job.task is not None and not job.running:
                job.task.cancel()
        _, pending = await asyncio.wait(tasks, timeout=self.grace) if tasks else (set(), set())
        if pending:
            logger.warning("Cancelling %d scheduled jobs still running", len(pending))
            for task in pending:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in self._jobs.values():
            job.task, job.next_run = None, None
        self._started = False

    @asynccontextmanager
    async def lifespan(self, app: Any = None) -> AsyncIterator[None]:
        """For `FastAPI(lifespan=scheduler.lifespan)`: runs the jobs while the app runs."""
        await self.start()
        try:
            yield
        finally:
            await self.stop()

    async def __aenter__(self) -> "FastAPIScheduler":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    def _resolve(self) -> Cache:
        if self.cache is not None:
            return self.cache
        from fastapi_alcazaba import FastAPICache

        return FastAPICache.instance()

    def _metrics(self) -> Metrics:
        if self._resolved is not None:
            return self._resolved.metrics
        from fastapi_alcazaba import FastAPICache

        return FastAPICache.get_metrics()

    async def _loop(self, job: _Job) -> None:
        while not self._stopping:
            job.next_run = tick = job.trigger.next_after(datetime.now(UTC))
            # In steps, so that a clock that is set while it waits is noticed.
            while (left := tick.timestamp() - time.time()) > 0:
                await asyncio.sleep(min(left, 60))
            job.running = True
            try:
                await self._tick(job, tick)
            finally:
                job.running = False

    async def _tick(self, job: _Job, tick: datetime) -> None:
        metrics = self._metrics()
        lock: Lock | None = None
        if job.exclusive:
            assert self._resolved is not None  # noqa: S101  # resolved at start()
            cache = self._resolved
            try:
                if not await self._claim(cache, job, tick):
                    metrics.scheduler_run("skipped", job.name)
                    return
                lock = self._run_lock(cache, job)
                if lock is not None and not await lock.acquire():
                    logger.info("Scheduled job %r skipped: its last run is still going", job.name)
                    metrics.scheduler_run("overlapping", job.name)
                    return
            except BackendUnavailable as exc:
                metrics.scheduler_run("unchecked", job.name)
                if not self.fail_open:
                    logger.warning(
                        "Scheduled job %r skipped: the backend failed", job.name, exc_info=exc
                    )
                    return
                logger.warning(
                    "Scheduled job %r runs unchecked: the backend failed", job.name, exc_info=exc
                )
                lock = None
        try:
            if _is_async(job.func):
                await job.func()
            else:
                await asyncio.to_thread(job.func)
        except Exception:
            logger.exception("Scheduled job %r failed", job.name)
            metrics.scheduler_run("failed", job.name)
        else:
            metrics.scheduler_run("ran", job.name)
        finally:
            if lock is not None:
                await lock.release()

    async def _claim(self, cache: Cache, job: _Job, tick: datetime) -> bool:
        """Whether this process is the first to claim the tick."""
        digest = hashlib.sha256(job.name.encode()).hexdigest()[:32]
        key = cache.state_key("scheduler", f"{{{digest}}}:{round(tick.timestamp() * 1000)}")
        # Long enough for the processes whose clocks are a little behind; at most an hour.
        gap = (job.trigger.next_after(tick) - tick).total_seconds()
        expire = math.ceil(min(max(gap, 60), 3600))
        claims = await cache.guard.run("incr", lambda: cache.backend.incr(key, 1, expire))
        return claims == 1

    def _run_lock(self, cache: Cache, job: _Job) -> Lock | None:
        try:
            return cache.lock(f"scheduler:{job.name}", ttl=_RUN_LOCK_TTL)
        except NotImplementedError:
            return None  # memcached: each tick still runs once, but long runs may overlap
