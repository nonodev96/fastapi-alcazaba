"""Gauges of backlogs, breakers and leaders, the scheduler's durations, and the BacklogMonitor,
read with OpenTelemetry's in-memory reader."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import logging
import types
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from fastapi_alcazaba import Cache, FastAPIBreaker, FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.background import FastAPILeader, FastAPIScheduler
from fastapi_alcazaba.background import outbox as outbox_module
from fastapi_alcazaba.observability import BacklogMonitor
from fastapi_alcazaba.telemetry.metrics import GAUGES, Metrics
from tests.unit.test_metrics import points
from tests.unit.test_outbox import Shop
from tests.unit.test_tasks import World, eventually


@pytest.fixture(name="reader")
def fixture_reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


def provider(reader: InMemoryMetricReader) -> MeterProvider:
    return MeterProvider(metric_readers=[reader])


def gauge(reader: InMemoryMetricReader, name: str) -> dict[tuple[tuple[str, Any], ...], Any]:
    """The gauge's last values, by their attributes."""
    return {
        tuple(sorted(attributes.items())): value
        for attributes, value in points(reader, f"fastapi_alcazaba.{name}")
    }


class TestMetrics:
    def test_set(self, reader: InMemoryMetricReader) -> None:
        metrics = Metrics(meter_provider=provider(reader))
        metrics.set("tasks.dead_letters", 3, queue="emails")
        metrics.set("tasks.dead_letters", 1, queue="emails")
        metrics.set("tasks.dead_letters", 7, queue="reports")
        assert gauge(reader, "tasks.dead_letters") == {
            (("fastapi_alcazaba.queue", "emails"),): 1,
            (("fastapi_alcazaba.queue", "reports"),): 7,
        }

    def test_unknown_gauges(self) -> None:
        with pytest.raises(ValueError, match="unknown gauge"):
            Metrics(enabled=False).set("tasks.whatever", 1)

    def test_disabled(self) -> None:
        Metrics(enabled=False).set("tasks.dead_letters", 1, queue="q")  # nothing to do

    def test_every_gauge_has_a_unit_and_a_description(self) -> None:
        for unit, description in GAUGES.values():
            assert unit
            assert len(description) > 20


class TestBreaker:
    async def test_open_and_closed(self, reader: InMemoryMetricReader) -> None:
        breaker = FastAPIBreaker(
            "payments",
            failure_threshold=1,
            reset_timeout=0.05,
            metrics=Metrics(meter_provider=provider(reader)),
        )

        @breaker
        async def call(fail: bool) -> None:
            if fail:
                raise ConnectionError

        with pytest.raises(ConnectionError):
            await call(True)
        assert gauge(reader, "breaker.open") == {(("fastapi_alcazaba.breaker", "payments"),): 1}
        await asyncio.sleep(0.06)
        await call(False)  # the trial goes through
        assert gauge(reader, "breaker.open") == {(("fastapi_alcazaba.breaker", "payments"),): 0}
        with pytest.raises(ConnectionError):
            await call(True)
        breaker.reset()
        assert gauge(reader, "breaker.open") == {(("fastapi_alcazaba.breaker", "payments"),): 0}


class TestLeader:
    async def test_one_process_leads(self) -> None:
        backend = InMemoryBackend()
        readers = [InMemoryMetricReader(), InMemoryMetricReader()]
        leaders = [
            FastAPILeader("relay", ttl=0.3, cache=Cache(backend, meter_provider=provider(reader)))
            for reader in readers
        ]

        def values() -> list[Any]:
            return [gauge(reader, "leader.is_leader").get(KEY) for reader in readers]

        await leaders[0].start()
        await eventually(lambda: leaders[0].is_leader)
        await leaders[1].start()
        assert values() == [1, 0]
        await leaders[0].stop()
        assert values()[0] == 0
        await eventually(lambda: leaders[1].is_leader)
        assert values() == [0, 1]
        await leaders[1].stop()
        assert values() == [0, 0]


KEY = (("fastapi_alcazaba.leader", "relay"),)


class TestScheduler:
    async def test_durations(self, reader: InMemoryMetricReader) -> None:
        FastAPICache.init(InMemoryBackend(), meter_provider=provider(reader))
        scheduler = FastAPIScheduler()

        @scheduler.every(seconds=0.05, exclusive=False, name="ok")
        async def ok() -> None:
            await asyncio.sleep(0.01)

        @scheduler.every(seconds=0.05, exclusive=False, name="broken")
        def broken() -> None:
            raise ValueError("no")

        await scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop()
        found = {
            (attributes["fastapi_alcazaba.job"], attributes["fastapi_alcazaba.outcome"]): count
            for attributes, count in points(reader, "fastapi_alcazaba.scheduler.duration")
        }
        assert found[("ok", "ran")] >= 1
        assert found[("broken", "failed")] >= 1


class TestTaskQueue:
    async def test_counts_and_measure(self, reader: InMemoryMetricReader) -> None:
        world = World()
        queue = world.queue(world.cache(meter_provider=provider(reader)), name="emails")

        @queue.task("job")
        async def job() -> None: ...

        await queue.enqueue(job)
        await queue.enqueue(job)
        await queue.enqueue(job, delay=60)
        await world.plant(queue, {"p": b"not a task"})
        assert await queue.counts() == {"queued": 3, "delayed": 1, "dead_letters": 0}
        async with queue:
            await eventually(queue.dead_letter_count)
            await eventually(lambda: _queued(queue, 0))
        await queue.measure()
        assert gauge(reader, "tasks.backlog") == {
            (("fastapi_alcazaba.queue", "emails"), ("fastapi_alcazaba.state", "delayed")): 1,
            (("fastapi_alcazaba.queue", "emails"), ("fastapi_alcazaba.state", "queued")): 0,
        }
        assert gauge(reader, "tasks.dead_letters") == {(("fastapi_alcazaba.queue", "emails"),): 1}


async def _queued(queue: Any, count: int) -> bool:
    return bool((await queue.counts())["queued"] == count)


@pytest.fixture(name="shop")
def fixture_shop() -> Iterator[Shop]:
    shop = Shop()
    yield shop
    shop.close()


class TestOutbox:
    async def test_backlog_and_measure(
        self,
        shop: Shop,
        reader: InMemoryMetricReader,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        FastAPICache.init(InMemoryBackend(), meter_provider=provider(reader))
        assert await shop.outbox.backlog() == (0, None)
        await shop.outbox.measure()
        assert gauge(reader, "outbox.oldest_age") == {(("fastapi_alcazaba.outbox", "outbox"),): 0}
        first = await shop.place("olive oil")
        await shop.place("bread")
        count, oldest = await shop.outbox.backlog()
        assert count == 2
        assert oldest == min(row["created"] for row in shop.rows())
        await shop.outbox.sent(first)
        assert (await shop.outbox.backlog())[0] == 1
        later = oldest + 90  # type: ignore[operator]
        monkeypatch.setattr(outbox_module, "time", types.SimpleNamespace(time=lambda: later))
        await shop.outbox.measure()
        assert gauge(reader, "outbox.backlog") == {(("fastapi_alcazaba.outbox", "outbox"),): 1}
        (age,) = gauge(reader, "outbox.oldest_age").values()
        assert 85 <= age <= 90


class Source:
    def __init__(self, behaviour: str = "ok") -> None:
        self.behaviour = behaviour
        self.measured = 0

    async def measure(self) -> None:
        self.measured += 1
        if self.behaviour == "fails":
            raise ConnectionError("down")
        if self.behaviour == "hangs":
            await asyncio.Event().wait()

    def __repr__(self) -> str:
        return f"Source({self.behaviour})"


class TestBacklogMonitor:
    async def test_measures_every_interval(self) -> None:
        source = Source()
        async with BacklogMonitor(source, interval=0.05):
            await eventually(lambda: source.measured >= 3)
        measured = source.measured
        await asyncio.sleep(0.1)
        assert source.measured == measured  # stopped

    async def test_a_failing_or_hanging_source_doesnt_stop_the_others(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING)
        good, failing, hanging = Source(), Source("fails"), Source("hangs")
        monitor = BacklogMonitor(failing, hanging, good, interval=0.05, timeout=0.05)
        await monitor.start()
        await eventually(lambda: good.measured >= 2)
        await monitor.stop()
        assert failing.measured >= 2
        assert "Couldn't measure the backlog of Source(fails)" in caplog.text
        assert "Couldn't measure the backlog of Source(hangs)" in caplog.text

    async def test_lifespan_and_once(self) -> None:
        source = Source()
        monitor = BacklogMonitor(source, interval=60)
        await monitor.measure()
        assert source.measured == 1
        async with monitor.lifespan():
            await eventually(lambda: source.measured == 2)
            with pytest.raises(RuntimeError, match="already running"):
                await monitor.start()
        await monitor.stop()  # stopped already: nothing to do

    @pytest.mark.parametrize(
        ("sources", "options", "error"),
        [
            ((), {}, ValueError),
            ((object(),), {}, TypeError),
            ((Source(),), {"interval": 0}, ValueError),
            ((Source(),), {"timeout": -1}, ValueError),
        ],
    )
    def test_invalid(
        self,
        sources: tuple[Any, ...],
        options: dict[str, Any],
        error: type[Exception],
    ) -> None:
        with pytest.raises(error):
            BacklogMonitor(*sources, **options)
