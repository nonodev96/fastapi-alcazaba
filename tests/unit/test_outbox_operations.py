"""The outbox in production: purging, messages given up on, traces and metrics, the relay's loop,
and the async sessions."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.background import outbox as outbox_module
from fastapi_alcazaba.background.outbox import (
    GivenUpMessage,
    OutboxMessage,
    OutboxRelay,
    SQLAlchemyOutbox,
    outbox_table,
)
from fastapi_alcazaba.telemetry.tracing import Tracer, traceparent
from tests.unit.test_metrics import points
from tests.unit.test_outbox import Clock, Shop

DAY = 24 * 3600


@pytest.fixture(name="clock", autouse=True)
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(outbox_module, "time", clock)
    return clock


@pytest.fixture(name="shop")
def fixture_shop() -> Iterator[Shop]:
    shop = Shop()
    yield shop
    shop.close()


async def give_up(shop: Shop, clock: Clock, *items: str) -> list[str]:
    """Messages for `items` that the relay gives up on."""
    ids = [await shop.place(item) for item in items]
    shop.failing = 100
    relay = OutboxRelay(shop.outbox, shop.deliver, max_attempts=1)
    await relay.relay_once()
    shop.failing = 0
    clock.now += 1
    return ids


class TestPurge:
    async def test_only_what_was_sent_long_ago(self, shop: Shop, clock: Clock) -> None:
        old = await shop.place("old")
        relay = OutboxRelay(shop.outbox, shop.deliver, retention=7 * DAY)
        await relay.relay_once()
        clock.now += 6 * DAY
        recent = await shop.place("recent")
        await relay.relay_once()
        (given_up,) = await give_up(shop, clock, "given up")
        pending = await shop.place("pending")
        clock.now += 1.5 * DAY  # old: 7.5 days; the others, 1.5 days at most
        assert await relay.purge() == 1
        assert {row["id"] for row in shop.rows()} == {recent, given_up, pending}
        assert old not in {row["id"] for row in shop.rows()}
        clock.now += 100 * DAY
        assert await relay.purge() == 1  # recent; the one given up on and the pending one stay
        assert {row["id"] for row in shop.rows()} == {given_up, pending}

    async def test_in_batches(self, shop: Shop, clock: Clock) -> None:
        for n in range(7):
            await shop.place(f"item {n}")
        await OutboxRelay(shop.outbox, shop.deliver).relay_once()
        clock.now += 10
        deletes: list[int] = []
        original = shop.outbox._delete

        async def counted(ids: list[str]) -> None:
            deletes.append(len(ids))
            await original(ids)

        shop.outbox._delete = counted  # type: ignore[method-assign]
        assert await shop.outbox.purge(5, batch=3) == 7
        assert deletes == [3, 3, 1]
        assert shop.rows() == []

    async def test_kept_without_a_retention(self, shop: Shop, clock: Clock) -> None:
        await shop.place("kept")
        relay = OutboxRelay(shop.outbox, shop.deliver, retention=None)
        await relay.relay_once()
        clock.now += 1000 * DAY
        assert await relay.purge() == 0
        assert len(shop.rows()) == 1

    @pytest.mark.parametrize(("older_than", "batch"), [(-1, 10), (10, 0)])
    async def test_invalid(self, shop: Shop, older_than: float, batch: int) -> None:
        with pytest.raises(ValueError, match="older_than"):
            await shop.outbox.purge(older_than, batch=batch)


class TestGivenUp:
    async def test_listed_requeued_and_discarded(self, shop: Shop, clock: Clock) -> None:
        first, second, third = await give_up(shop, clock, "a", "b", "c")
        delivered = await shop.place("d")
        await OutboxRelay(shop.outbox, shop.deliver).relay_once()
        listed = await shop.outbox.given_up()
        assert {message.id for message in listed} == {first, second, third}
        assert all(isinstance(message, GivenUpMessage) for message in listed)
        assert listed[0].attempts == 1
        assert "ConnectionError" in (listed[0].error or "")
        assert not hasattr(listed[0], "payload")  # what the message says isn't shown
        assert len(await shop.outbox.given_up(limit=2)) == 2
        assert await shop.outbox.requeue([first, delivered, "unknown"]) == 1
        assert await shop.outbox.discard([second, delivered]) == 1
        assert await shop.outbox.requeue([]) == 0
        assert await shop.outbox.discard([]) == 0
        assert {message.id for message in await shop.outbox.given_up()} == {third}
        shop.delivered.clear()
        assert await OutboxRelay(shop.outbox, shop.deliver).relay_once() == 1
        assert [message.id for message in shop.delivered] == [first]
        assert shop.delivered[0].attempts == 0  # its attempts reset
        assert await shop.outbox.requeue() == 1  # all of them: the third
        assert await shop.outbox.given_up() == []
        assert delivered in {row["id"] for row in shop.rows()}  # sent, untouched


@pytest.fixture(name="tracing")
def fixture_tracing() -> tuple[InMemorySpanExporter, TracerProvider]:
    """FastAPICache with a tracer provider whose spans are kept in memory."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    FastAPICache.init(InMemoryBackend(), tracer_provider=provider)
    return exporter, provider


class TestTraces:
    async def test_a_delivery_links_to_the_request(
        self,
        shop: Shop,
        clock: Clock,
        tracing: tuple[InMemorySpanExporter, TracerProvider],
    ) -> None:
        spans, provider = tracing
        tracer = provider.get_tracer("app")
        with tracer.start_as_current_span("POST /orders") as request:
            message_id = await shop.place("olive oil")
        stored = shop.rows()[0]["traceparent"]
        assert stored.split("-")[1] == f"{request.get_span_context().trace_id:032x}"
        seen: list[str | None] = []

        async def deliver(message: OutboxMessage) -> None:
            del message
            seen.append(traceparent())  # what a task enqueued here would carry

        shop.failing = 1
        relay = OutboxRelay(shop.outbox, shop.deliver)
        await relay.relay_once()  # fails
        relay = OutboxRelay(shop.outbox, deliver)
        clock.now += 5
        await relay.relay_once()
        deliveries = [
            span for span in spans.get_finished_spans() if span.name.endswith("order.placed")
        ]
        assert len(deliveries) == 2
        failed, delivered = deliveries
        for span in deliveries:
            assert span.name == "fastapi_alcazaba.outbox order.placed"
            assert span.parent is None  # the root of a trace of its own
            assert [link.context.span_id for link in span.links] == [
                request.get_span_context().span_id
            ]
            assert span.attributes is not None
            assert span.attributes["fastapi_alcazaba.message_id"] == message_id
        assert failed.status.status_code.name == "ERROR"
        assert delivered.attributes is not None
        assert delivered.attributes["fastapi_alcazaba.attempt"] == 1
        assert seen[0] is not None
        assert seen[0].split("-")[2] == f"{delivered.context.span_id:016x}"

    async def test_without_a_span_nor_a_valid_one(
        self,
        shop: Shop,
        tracing: tuple[InMemorySpanExporter, TracerProvider],
    ) -> None:
        spans, _ = tracing
        await shop.place("no span")
        assert shop.rows()[0]["traceparent"] is None
        with shop.engine.begin() as connection:
            connection.execute(shop.table.update().values(traceparent="00-not-a-trace"))
        assert await OutboxRelay(shop.outbox, shop.deliver).relay_once() == 1
        (span,) = spans.get_finished_spans()
        assert span.links == ()


class TestMetrics:
    async def test_counted(self, shop: Shop, clock: Clock) -> None:
        reader = InMemoryMetricReader()
        FastAPICache.init(InMemoryBackend(), meter_provider=MeterProvider(metric_readers=[reader]))
        await give_up(shop, clock, "b")
        await shop.place("a")
        await shop.place("c")
        shop.failing = 1  # a fails once
        relay = OutboxRelay(shop.outbox, shop.deliver, max_attempts=5, retention=0)
        await relay.relay_once()
        clock.now += 10
        await relay.relay_once()
        clock.now += 10
        await relay.purge()
        found = {
            attributes["fastapi_alcazaba.outcome"]: count
            for attributes, count in points(reader, "fastapi_alcazaba.outbox")
        }
        assert found == {"given_up": 1, "delivered": 2, "retried": 1}
        assert points(reader, "fastapi_alcazaba.outbox.purged") == [
            ({"fastapi_alcazaba.outbox": "outbox"}, 2)
        ]
        topics = {a["fastapi_alcazaba.topic"] for a, _ in points(reader, "fastapi_alcazaba.outbox")}
        assert topics == {"order.placed"}


class TestRun:
    async def test_delivers_purges_and_goes_on(
        self,
        shop: Shop,
        clock: Clock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO)
        await shop.place("old")
        await OutboxRelay(shop.outbox, shop.deliver).relay_once()
        clock.now += 8 * DAY
        await shop.place("new")
        relay = OutboxRelay(shop.outbox, shop.deliver, interval=0.01, purge_interval=3600)
        task = asyncio.get_running_loop().create_task(relay.run())
        await _until(lambda: len(shop.delivered) == 2)
        assert [row["topic"] for row in shop.rows()] == ["order.placed"]  # old was purged
        assert "Purged 1 messages" in caplog.text
        # The database fails: the relay logs it and goes on.
        broken = shop.outbox.due

        async def down(limit: int) -> list[OutboxMessage]:
            raise ConnectionError("the database is down")

        shop.outbox.due = down  # type: ignore[method-assign]
        await asyncio.sleep(0.05)
        assert "Couldn't read the outbox" in caplog.text
        shop.outbox.due = broken  # type: ignore[method-assign]
        await shop.place("later")
        await _until(lambda: len(shop.delivered) == 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_purges_once_per_interval_and_survives_failures(
        self,
        shop: Shop,
        clock: Clock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        relay = OutboxRelay(shop.outbox, shop.deliver, purge_interval=60)
        purges = [0]

        async def failing(older_than: float, *, batch: int = 1000) -> int:
            del older_than, batch
            purges[0] += 1
            raise ConnectionError("down")

        shop.outbox.purge = failing  # type: ignore[method-assign]
        await relay._purge_when_due()
        await relay._purge_when_due()
        assert purges == [1]
        assert "Couldn't purge the outbox" in caplog.text
        clock.now += 61
        await relay._purge_when_due()
        assert purges == [2]

    @pytest.mark.parametrize(
        "options",
        [{"retention": -1}, {"purge_interval": 0}, {"purge_batch": 0}],
    )
    def test_invalid(self, shop: Shop, options: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="retention"):
            OutboxRelay(shop.outbox, shop.deliver, **options)


async def _until(condition: Any, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


class TestTable:
    def test_indexes_and_columns(self) -> None:
        table = outbox_table(MetaData(), "messages")
        indexes = {index.name: [column.name for column in index.columns] for index in table.indexes}
        assert indexes == {
            "ix_messages_due": ["sent", "next_try"],
            "ix_messages_failed": ["failed"],
        }
        assert "traceparent" in table.c


@pytest.fixture(name="async_outbox")
async def fixture_async_outbox() -> AsyncIterator[SQLAlchemyOutbox]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    metadata = MetaData()
    table = outbox_table(metadata)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    yield SQLAlchemyOutbox(table, async_sessionmaker(engine))
    await engine.dispose()


class TestAsyncSessions:
    async def test_everything(self, async_outbox: SQLAlchemyOutbox, clock: Clock) -> None:
        sessions = async_outbox.sessions
        async with sessions() as session, session.begin():
            sent_id = await async_outbox.add(session, "order.placed", {"n": 1})
            failing_id = await async_outbox.add(session, "order.placed", {"n": 2})
        delivered: list[OutboxMessage] = []

        async def deliver(message: OutboxMessage) -> None:
            if message.id == failing_id:
                raise ConnectionError("down")
            delivered.append(message)

        relay = OutboxRelay(async_outbox, deliver, max_attempts=1)
        assert await relay.relay_once() == 1
        assert [message.payload for message in delivered] == [{"n": 1}]
        assert await async_outbox.backlog() == (0, None)
        assert [message.id for message in await async_outbox.given_up()] == [failing_id]
        assert await async_outbox.requeue([failing_id]) == 1
        assert (await async_outbox.backlog())[0] == 1
        assert await async_outbox.discard([failing_id]) == 0  # not given up on any more
        clock.now += 8 * DAY
        assert await async_outbox.purge(7 * DAY) == 1
        async with sessions() as session:
            ids = (await session.execute(select(async_outbox.table.c.id))).scalars().all()
        assert list(ids) == [failing_id]
        assert sent_id not in ids


class TestEdges:
    async def test_a_payload_too_large(self, shop: Shop) -> None:
        with shop.sessions() as session, pytest.raises(ValueError, match="262144 bytes"):
            await shop.outbox.add(session, "big", "x" * (300 * 1024))

    async def test_a_payload_that_isnt_json_any_more(self, shop: Shop) -> None:
        await shop.place("olive oil")
        with shop.engine.begin() as connection:
            connection.execute(shop.table.update().values(payload="{not json"))
        (message,) = await shop.outbox.due(10)
        assert message.payload is None

    def test_no_span_when_tracing_is_off(self) -> None:
        with Tracer(enabled=False).outbox("outbox", "t", "id", 0, None) as span:
            assert span is None
