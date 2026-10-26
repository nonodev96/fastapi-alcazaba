"""FastAPIScheduler: jobs every N seconds or by cron, each tick once across the processes."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import logging
import threading
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from fastapi_alcazaba import Cache, FastAPICache, FastAPIScheduler
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.scheduler import _Cron, _Every
from fastapi_alcazaba.types import Backend
from tests.unit.test_metrics import points

MADRID = ZoneInfo("Europe/Madrid")
TICK = 0.1  # seconds between the ticks of the scheduling tests


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def runs(expression: str, after: str, n: int, tz: ZoneInfo | None = None) -> list[str]:
    """The next `n` times of `expression`, in UTC."""
    cron, moment, found = _Cron(expression, tz or ZoneInfo("UTC")), utc(after), []
    for _ in range(n):
        moment = cron.next_after(moment)
        found.append(moment.strftime("%Y-%m-%d %H:%M"))
    return found


class TestCronExpressions:
    @pytest.mark.parametrize(
        ("expression", "after", "expected"),
        [
            ("*/15 * * * *", "2026-10-26 10:07", ["2026-10-26 10:15", "2026-10-26 10:30"]),
            ("0 9-17/4 * * *", "2026-10-26 10:00", ["2026-10-26 13:00", "2026-10-26 17:00"]),
            ("5,35 0 * * *", "2026-10-26 00:05", ["2026-10-26 00:35", "2026-10-27 00:05"]),
            ("0 9 * * mon-fri", "2026-10-30 09:00", ["2026-11-02 09:00", "2026-11-03 09:00"]),
            ("0 0 * * 7", "2026-10-26 00:00", ["2026-11-01 00:00", "2026-11-08 00:00"]),
            ("0 0 1 jan,JUL *", "2026-10-26 00:00", ["2027-01-01 00:00", "2027-07-01 00:00"]),
            ("0 0 29 2 *", "2026-01-01 00:00", ["2028-02-29 00:00", "2032-02-29 00:00"]),
            ("0 0 30/10 * *", "2026-10-26 00:00", ["2026-10-30 00:00", "2026-11-30 00:00"]),
            ("@hourly", "2026-10-26 10:59", ["2026-10-26 11:00", "2026-10-26 12:00"]),
            ("@daily", "2026-10-26 10:00", ["2026-10-27 00:00", "2026-10-28 00:00"]),
            ("@weekly", "2026-10-26 10:00", ["2026-11-01 00:00", "2026-11-08 00:00"]),
            ("@monthly", "2026-10-26 10:00", ["2026-11-01 00:00", "2026-12-01 00:00"]),
            ("@yearly", "2026-10-26 10:00", ["2027-01-01 00:00", "2028-01-01 00:00"]),
        ],
    )
    def test_next_times(self, expression: str, after: str, expected: list[str]) -> None:
        assert runs(expression, after, 2) == expected

    def test_a_day_of_the_month_or_of_the_week_when_both_are_given(self) -> None:
        # The 13th or a Friday, as in every cron.
        assert runs("0 0 13 * fri", "2026-11-01 00:00", 3) == [
            "2026-11-06 00:00",
            "2026-11-13 00:00",
            "2026-11-20 00:00",
        ]

    @pytest.mark.parametrize(
        ("expression", "reason"),
        [
            ("* * * *", "5 fields"),
            ("60 * * * *", "between 0 and 59"),
            ("* 24 * * *", "between 0 and 23"),
            ("* * 0 * *", "between 1 and 31"),
            ("* * * 13 *", "between 1 and 12"),
            ("* * * * 8", "between 0 and 7"),
            ("*/0 * * * *", "isn't a step"),
            ("*/x * * * *", "isn't a step"),
            ("5-1 * * * *", "goes backwards"),
            ("a * * * *", "isn't a number"),
            ("1,,2 * * * *", "isn't a number"),
            ("0 0 30 2 *", "never matches"),
        ],
    )
    def test_invalid_expressions(self, expression: str, reason: str) -> None:
        with pytest.raises(ValueError, match=reason):
            _Cron(expression, ZoneInfo("UTC"))


class TestClockChanges:
    """Madrid in 2026: 29 March, 02:00 CET → 03:00 CEST; 25 October, 03:00 CEST → 02:00 CET."""

    def test_a_fixed_time_in_a_skipped_hour_runs_an_hour_later(self) -> None:
        # 02:30 doesn't exist on 29 March: 03:30 CEST, then 02:30 CEST on the 30th.
        assert runs("30 2 * * *", "2026-03-28 12:00", 2, MADRID) == [
            "2026-03-29 01:30",
            "2026-03-30 00:30",
        ]

    def test_a_fixed_time_in_a_repeated_hour_runs_once(self) -> None:
        # 02:30 CEST on the 25th (its first 02:30), then 02:30 CET on the 26th.
        assert runs("30 2 * * *", "2026-10-24 12:00", 2, MADRID) == [
            "2026-10-25 00:30",
            "2026-10-26 01:30",
        ]

    def test_wildcards_follow_real_time_through_a_repeated_hour(self) -> None:
        assert runs("*/30 * * * *", "2026-10-24 23:50", 5, MADRID) == [
            "2026-10-25 00:00",  # 02:00 CEST
            "2026-10-25 00:30",
            "2026-10-25 01:00",  # 02:00 CET
            "2026-10-25 01:30",
            "2026-10-25 02:00",  # 03:00 CET
        ]
        assert runs("0 * * * *", "2026-10-24 23:30", 3, MADRID) == [
            "2026-10-25 00:00",
            "2026-10-25 01:00",
            "2026-10-25 02:00",
        ]

    def test_wildcards_follow_real_time_through_a_skipped_hour(self) -> None:
        assert runs("*/30 * * * *", "2026-03-29 00:40", 3, MADRID) == [
            "2026-03-29 01:00",  # 03:00 CEST
            "2026-03-29 01:30",
            "2026-03-29 02:00",
        ]

    def test_from_a_moment_in_the_repeated_hour(self) -> None:
        # 02:45 CET, the second 02:45: the fixed 02:30 already ran in the first.
        cron = _Cron("30 2 * * *", MADRID)
        assert cron.next_after(utc("2026-10-25 01:45")) == utc("2026-10-26 01:30")


class TestEvery:
    def test_ticks_are_multiples_of_the_interval(self) -> None:
        every = _Every(300)
        assert every.next_after(utc("2026-10-26 10:07:30")) == utc("2026-10-26 10:10")
        assert every.next_after(utc("2026-10-26 10:10")) == utc("2026-10-26 10:15")


@pytest.fixture(name="shared", params=["memory", "redis"])
def fixture_shared(request: pytest.FixtureRequest) -> Backend:
    """The backend that several processes share."""
    if request.param == "memory":
        return InMemoryBackend()
    return RedisBackend(fakeredis.FakeAsyncRedis())


def processes(backend: Backend, n: int = 2, *, fail_open: bool = False) -> list[FastAPIScheduler]:
    """Schedulers of `n` processes sharing `backend`."""
    return [FastAPIScheduler(Cache(backend, prefix="app"), fail_open=fail_open) for _ in range(n)]


async def run_for(seconds: float, *schedulers: FastAPIScheduler) -> None:
    for scheduler in schedulers:
        await scheduler.start()
    await asyncio.sleep(seconds)
    for scheduler in schedulers:
        await scheduler.stop()


class TestScheduling:
    async def test_each_tick_runs_once_across_processes(self, shared: Backend) -> None:
        ticks: list[datetime | None] = []
        schedulers = processes(shared)
        for scheduler in schedulers:

            @scheduler.every(seconds=TICK, name="job")
            async def job(scheduler: FastAPIScheduler = scheduler) -> None:
                ticks.append(scheduler.next_run("job"))  # the tick being run

        await run_for(5.5 * TICK, *schedulers)
        assert len(ticks) >= 4
        assert len(ticks) == len(set(ticks))

    async def test_not_exclusive_runs_in_every_process(self) -> None:
        count = [0, 0]
        schedulers = [FastAPIScheduler(), FastAPIScheduler()]  # no backend needed
        for index, scheduler in enumerate(schedulers):

            @scheduler.every(seconds=TICK, exclusive=False)
            async def job(index: int = index) -> None:
                count[index] += 1

        await run_for(3.5 * TICK, *schedulers)
        assert min(count) >= 2

    async def test_a_long_run_doesnt_overlap_the_next_one_elsewhere(self, shared: Backend) -> None:
        running, overlaps, done = [0], [0], [0]
        schedulers = processes(shared)
        for scheduler in schedulers:

            @scheduler.every(seconds=TICK, name="slow")
            async def slow() -> None:
                running[0] += 1
                overlaps[0] = max(overlaps[0], running[0])
                await asyncio.sleep(2.5 * TICK)
                running[0] -= 1
                done[0] += 1

        await run_for(7 * TICK, *schedulers)
        assert overlaps[0] == 1
        assert done[0] >= 1

    async def test_sync_jobs_run_in_a_thread(self) -> None:
        threads: list[threading.Thread] = []
        scheduler = FastAPIScheduler()

        @scheduler.every(seconds=TICK, exclusive=False)
        def job() -> None:
            threads.append(threading.current_thread())

        await run_for(1.5 * TICK, scheduler)
        assert threads
        assert threading.main_thread() not in threads

    async def test_callable_objects(self) -> None:
        calls: list[int] = []

        class Job:
            async def __call__(self) -> None:
                calls.append(1)

        scheduler = FastAPIScheduler()
        scheduler.every(seconds=TICK, exclusive=False, name="object")(Job())
        await run_for(1.5 * TICK, scheduler)
        assert calls

    async def test_a_failing_job_is_logged_and_keeps_its_schedule(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        calls: list[int] = []
        scheduler = FastAPIScheduler()

        @scheduler.every(seconds=TICK, exclusive=False, name="flaky")
        async def flaky() -> None:
            calls.append(1)
            raise RuntimeError("boom")

        with caplog.at_level(logging.ERROR, "fastapi_alcazaba.scheduler"):
            await run_for(2.5 * TICK, scheduler)
        assert len(calls) >= 2
        assert "Scheduled job 'flaky' failed" in caplog.text
        assert "RuntimeError: boom" in caplog.text

    async def test_next_run(self) -> None:
        scheduler = FastAPIScheduler()
        scheduler.cron("0 3 * * *", tz="Europe/Madrid", exclusive=False, name="nightly")(noop)
        assert scheduler.next_run("nightly") is None
        async with scheduler:
            await asyncio.sleep(0)
            upcoming = scheduler.next_run("nightly")
            assert upcoming is not None
            assert upcoming.astimezone(MADRID).hour == 3
        assert scheduler.next_run("nightly") is None

    async def test_ticks_are_kept_apart_from_the_cache(self) -> None:
        backend = InMemoryBackend()
        (scheduler,) = processes(backend, n=1)
        scheduler.every(seconds=TICK, name="job")(noop)
        await run_for(1.5 * TICK, scheduler)
        assert all(key.startswith("app!") for key in backend._state)
        assert await Cache(backend, prefix="app").clear() == 0


class TestBackendFailures:
    async def test_the_tick_is_skipped_by_default(self, caplog: pytest.LogCaptureFixture) -> None:
        calls: list[int] = []
        (scheduler,) = processes(Down(), n=1)
        scheduler.every(seconds=TICK, name="job")(lambda: calls.append(1))
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.scheduler"):
            await run_for(1.5 * TICK, scheduler)
        assert not calls
        assert "Scheduled job 'job' skipped: the backend failed" in caplog.text

    async def test_failing_open(self, caplog: pytest.LogCaptureFixture) -> None:
        calls: list[int] = []
        (scheduler,) = processes(Down(), n=1, fail_open=True)
        scheduler.every(seconds=TICK, name="job")(lambda: calls.append(1))
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.scheduler"):
            await run_for(1.5 * TICK, scheduler)
        assert calls
        assert "runs unchecked" in caplog.text

    async def test_backends_without_locks(self) -> None:
        class WithoutLocks(InMemoryBackend):
            supports_locks = False

        calls: list[int] = []
        (scheduler,) = processes(WithoutLocks(), n=1)
        scheduler.every(seconds=TICK, name="job")(lambda: calls.append(1))
        await run_for(1.5 * TICK, scheduler)
        assert calls

    async def test_backends_without_counters(self) -> None:
        class WithoutCounters(InMemoryBackend):
            supports_counters = False

        (scheduler,) = processes(WithoutCounters(), n=1)
        scheduler.every(seconds=TICK)(noop)
        with pytest.raises(NotImplementedError, match="can't keep counters"):
            await scheduler.start()


class TestStopping:
    async def test_jobs_waiting_stop_at_once(self) -> None:
        scheduler = FastAPIScheduler()
        scheduler.every(hours=1, exclusive=False)(noop)
        await scheduler.start()
        start = time.monotonic()
        await scheduler.stop()
        assert time.monotonic() - start < 0.1
        await scheduler.stop()  # stopping twice is fine

    async def test_a_running_job_gets_its_grace(self) -> None:
        finished: list[bool] = []
        scheduler = FastAPIScheduler(grace=1)

        @scheduler.every(seconds=TICK, exclusive=False)
        async def job() -> None:
            await asyncio.sleep(0.15)
            finished.append(True)

        await scheduler.start()
        await asyncio.sleep(TICK + 0.05)  # running now
        await scheduler.stop()
        assert finished == [True]

    async def test_beyond_its_grace_it_is_cancelled(self, caplog: pytest.LogCaptureFixture) -> None:
        cancelled: list[bool] = []
        scheduler = FastAPIScheduler(grace=0.05)

        @scheduler.every(seconds=TICK, exclusive=False)
        async def job() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        await scheduler.start()
        await asyncio.sleep(TICK + 0.05)
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.scheduler"):
            await scheduler.stop()
        assert cancelled == [True]
        assert "Cancelling 1 scheduled jobs still running" in caplog.text

    def test_with_the_app(self) -> None:
        calls: list[int] = []
        FastAPICache.init(InMemoryBackend())
        scheduler = FastAPIScheduler()
        scheduler.every(seconds=TICK, name="job")(lambda: calls.append(1))
        with TestClient(FastAPI(lifespan=scheduler.lifespan)):
            time.sleep(2.5 * TICK)
        seen = len(calls)
        assert seen >= 1
        time.sleep(2 * TICK)
        assert len(calls) == seen  # stopped with the app


class TestRegistration:
    def test_the_default_name(self) -> None:
        scheduler = FastAPIScheduler()
        scheduler.every(minutes=1)(noop)
        assert list(scheduler._jobs) == ["tests.unit.test_scheduler.noop"]

    def test_mistakes(self) -> None:
        scheduler = FastAPIScheduler()
        scheduler.every(minutes=1, name="job")(noop)
        with pytest.raises(ValueError, match="already a job named 'job'"):
            scheduler.every(minutes=1, name="job")(noop)
        with pytest.raises(TypeError, match="must take no arguments"):
            scheduler.every(minutes=1, name="args")(lambda x: x)
        with pytest.raises(ValueError, match="interval"):
            scheduler.every()
        with pytest.raises(ValueError, match="cron expression"):
            scheduler.cron("not cron")
        with pytest.raises(ValueError, match="grace"):
            FastAPIScheduler(grace=-1)

    async def test_while_running(self) -> None:
        scheduler = FastAPIScheduler()
        async with scheduler:
            with pytest.raises(RuntimeError, match="already running"):
                await scheduler.start()
            with pytest.raises(RuntimeError, match="before the scheduler starts"):
                scheduler.every(minutes=1)(noop)

    async def test_exclusive_jobs_need_a_cache(self) -> None:
        scheduler = FastAPIScheduler()
        scheduler.every(minutes=1)(noop)
        with pytest.raises(RuntimeError, match=r"FastAPICache.init\(\) hasn't been called"):
            await scheduler.start()


async def test_metrics(shared: Backend) -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider([reader])
    caches = [Cache(shared, prefix="app", meter_provider=provider) for _ in range(2)]
    schedulers = [FastAPIScheduler(cache) for cache in caches]
    for scheduler in schedulers:
        scheduler.every(seconds=TICK, name="job")(noop)
    await run_for(2.5 * TICK, *schedulers)
    outcomes = {a["fastapi_alcazaba.outcome"] for a, _ in points(reader, RUNS)}
    assert outcomes == {"ran", "skipped"}


async def test_metrics_of_failures() -> None:
    reader = InMemoryMetricReader()
    FastAPICache.init(Down(), meter_provider=MeterProvider([reader]))
    scheduler = FastAPIScheduler()
    scheduler.every(seconds=TICK, name="job")(noop)
    scheduler.every(seconds=TICK, name="boom", exclusive=False)(boom)
    await run_for(1.5 * TICK, scheduler)
    outcomes = {
        (a["fastapi_alcazaba.job"], a["fastapi_alcazaba.outcome"]) for a, _ in points(reader, RUNS)
    }
    assert outcomes == {("job", "unchecked"), ("boom", "failed")}


RUNS = "fastapi_alcazaba.scheduler.runs"


async def noop() -> None:
    pass


async def boom() -> None:
    raise RuntimeError("boom")


class Down(InMemoryBackend):
    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        raise ConnectionError("down")
