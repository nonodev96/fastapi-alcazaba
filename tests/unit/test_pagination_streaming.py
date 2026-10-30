"""PaginationStreaming: every item as NDJSON, in chunks, from sync or async sources, and big
tables read by key a batch at a time."""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Awaitable, Iterator, MutableMapping
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from fastapi_alcazaba.pagination.streaming import PaginationStreaming


class Item(BaseModel):
    id: int


ITEMS = [Item(id=i) for i in range(1, 8)]


async def chunks(response: StreamingResponse) -> list[list[Any]]:
    """The chunks of `response`, each as the JSON values of its lines."""
    found = []
    async for chunk in response.body_iterator:
        assert isinstance(chunk, bytes)
        assert chunk.endswith(b"\n")
        found.append([json.loads(line) for line in chunk.splitlines()])
    return found


class Source:
    """A generator over ITEMS that counts what is read from it and knows whether it was closed."""

    def __init__(self) -> None:
        self.read = 0
        self.closed = False
        self.threads: set[int] = set()

    def sync(self) -> Iterator[Item]:
        try:
            for item in ITEMS:
                self.read += 1
                self.threads.add(threading.get_ident())
                yield item
        finally:
            self.closed = True

    async def async_(self) -> AsyncIterator[Item]:
        try:
            for item in ITEMS:
                self.read += 1
                yield item
        finally:
            self.closed = True


class TestResponse:
    async def test_ndjson_in_chunks_of_batch_size_lines(self) -> None:
        response = PaginationStreaming(batch_size=3).response(ITEMS)
        assert response.media_type == "application/x-ndjson"
        assert await chunks(response) == [
            [{"id": 1}, {"id": 2}, {"id": 3}],
            [{"id": 4}, {"id": 5}, {"id": 6}],
            [{"id": 7}],
        ]

    async def test_a_synchronous_source_is_read_in_the_thread_pool(self) -> None:
        source = Source()
        response = PaginationStreaming(batch_size=4).response(source.sync())
        assert [len(chunk) for chunk in await chunks(response)] == [4, 3]
        assert (source.read, source.closed) == (7, True)
        assert threading.get_ident() not in source.threads

    async def test_an_async_source(self) -> None:
        source = Source()
        response = PaginationStreaming(batch_size=5).response(source.async_())
        assert [len(chunk) for chunk in await chunks(response)] == [5, 2]
        assert (source.read, source.closed) == (7, True)

    @pytest.mark.parametrize("kind", ["sync", "async_"])
    async def test_max_items_ends_the_stream_and_closes_the_source(self, kind: str) -> None:
        source = Source()
        stream = PaginationStreaming(batch_size=2, max_items=5)
        response = stream.response(getattr(source, kind)())
        assert [[item["id"] for item in chunk] for chunk in await chunks(response)] == [
            [1, 2],
            [3, 4],
            [5],
        ]
        assert (source.read, source.closed) == (5, True)  # nothing read past the fifth

    async def test_max_items_at_the_end_of_a_chunk(self) -> None:
        response = PaginationStreaming(batch_size=3, max_items=6).response(ITEMS)
        assert [len(chunk) for chunk in await chunks(response)] == [3, 3]

    async def test_nothing_to_send(self) -> None:
        assert await chunks(PaginationStreaming().response([])) == []

    async def test_values_as_fastapi_serialises_them(self) -> None:
        class Event(BaseModel):
            event_id: int = Field(serialization_alias="eventId")
            at: datetime

        at = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
        response = PaginationStreaming().response([Event(event_id=1, at=at), {"n": 2}, 3, "x"])
        assert await chunks(response) == [
            [{"eventId": 1, "at": "2026-10-29T10:00:00Z"}, {"n": 2}, 3, "x"],
        ]

    def test_limits(self) -> None:
        with pytest.raises(ValueError, match="batch_size"):
            PaginationStreaming(batch_size=0)
        with pytest.raises(ValueError, match="max_items"):
            PaginationStreaming(max_items=0)


ROWS = [Item(id=i) for i in range(1, 101)]


class Table:
    """Stands in for a table with an index on id: what each query asked for."""

    def __init__(self, size: int) -> None:
        self.rows = ROWS[:size]
        self.queries: list[tuple[int | None, int]] = []
        self.threads: set[int] = set()

    def fetch(
        self,
        after: int | None,
        limit: int,
    ) -> list[Item]:
        """WHERE id > :after ORDER BY id LIMIT :limit"""
        self.queries.append((after, limit))
        self.threads.add(threading.get_ident())
        return [row for row in self.rows if after is None or row.id > after][:limit]

    async def fetch_async(
        self,
        after: int | None,
        limit: int,
    ) -> list[Item]:
        return self.fetch(after, limit)


async def read(stream: PaginationStreaming, table: Table, *, use_async: bool = False) -> list[int]:
    fetch = table.fetch_async if use_async else table.fetch
    return [row.id async for row in stream.batches(fetch, key=lambda row: row.id)]


class TestBatches:
    async def test_by_key_until_a_short_batch(self) -> None:
        table = Table(7)
        assert await read(PaginationStreaming(batch_size=3), table) == list(range(1, 8))
        assert table.queries == [(None, 3), (3, 3), (6, 3)]

    async def test_an_exact_multiple_ends_with_an_empty_batch(self) -> None:
        table = Table(6)
        assert await read(PaginationStreaming(batch_size=3), table) == list(range(1, 7))
        assert table.queries == [(None, 3), (3, 3), (6, 3)]

    async def test_an_empty_table(self) -> None:
        table = Table(0)
        assert await read(PaginationStreaming(batch_size=3), table) == []
        assert table.queries == [(None, 3)]

    async def test_async_fetch(self) -> None:
        table, stream = Table(5), PaginationStreaming(batch_size=2)
        assert await read(stream, table, use_async=True) == [1, 2, 3, 4, 5]
        assert table.queries == [(None, 2), (2, 2), (4, 2)]
        assert table.threads == {threading.get_ident()}

    async def test_a_synchronous_fetch_runs_in_the_thread_pool(self) -> None:
        table = Table(5)
        await read(PaginationStreaming(batch_size=2), table)
        assert threading.get_ident() not in table.threads

    async def test_an_async_callable_and_a_lambda_returning_a_coroutine(self) -> None:
        class Fetch:
            def __init__(self, table: Table) -> None:
                self.table = table

            async def __call__(
                self,
                after: int | None,
                limit: int,
            ) -> list[Item]:
                return self.table.fetch(after, limit)

        other = Table(5)

        def returning_a_coroutine(
            after: int | None,
            limit: int,
        ) -> Awaitable[list[Item]]:
            return other.fetch_async(after, limit)

        stream = PaginationStreaming(batch_size=4)
        for fetch in (Fetch(Table(5)), returning_a_coroutine):
            rows = stream.batches(fetch, key=lambda r: r.id)
            assert [row.id async for row in rows] == [1, 2, 3, 4, 5]

    async def test_a_composite_key(self) -> None:
        events = [(datetime(2026, 10, 29, 10, i // 2, tzinfo=UTC), i) for i in range(5)]
        asked: list[tuple[datetime, int] | None] = []

        def fetch(
            after: tuple[datetime, int] | None,
            limit: int,
        ) -> list[tuple[datetime, int]]:
            asked.append(after)
            return [event for event in events if after is None or event > after][:limit]

        stream = PaginationStreaming(batch_size=2)
        assert [event async for event in stream.batches(fetch, key=lambda e: e)] == events
        assert asked == [None, events[1], events[3]]

    async def test_a_fetch_that_doesnt_read_past_the_key(self) -> None:
        table = Table(10)
        stream = PaginationStreaming(batch_size=3)
        seen: list[int] = []

        async def walk() -> None:
            async for row in stream.batches(lambda _after, n: table.rows[:n], key=lambda r: r.id):
                seen.append(row.id)

        with pytest.raises(ValueError, match="past the key"):
            await walk()
        assert seen == [1, 2, 3]  # the repeated batch isn't sent

    async def test_streamed_with_max_items(self) -> None:
        table = Table(100)
        stream = PaginationStreaming(batch_size=10, max_items=25)
        response = stream.response(stream.batches(table.fetch, key=lambda row: row.id))
        assert [len(chunk) for chunk in await chunks(response)] == [10, 10, 5]
        assert table.queries == [(None, 10), (10, 10), (20, 10)]


class TestInAnApp:
    def test_the_whole_table(self) -> None:
        app, table = FastAPI(), Table(100)
        stream = PaginationStreaming(batch_size=30)

        @app.get("/items")
        async def items() -> StreamingResponse:
            return stream.response(stream.batches(table.fetch_async, key=lambda row: row.id))

        with TestClient(app) as client:
            response = client.get("/items")
        assert response.headers["content-type"] == "application/x-ndjson"
        assert [json.loads(line)["id"] for line in response.text.splitlines()] == list(
            range(1, 101)
        )
        assert [after for after, _ in table.queries] == [None, 30, 60, 90]

    async def test_a_client_that_disconnects_stops_the_stream(self) -> None:
        app, stream, source = FastAPI(), PaginationStreaming(batch_size=10), Source()

        async def endless() -> AsyncIterator[Item]:
            try:
                while True:
                    source.read += 1
                    yield Item(id=source.read)
                    await asyncio.sleep(0)
            finally:
                source.closed = True

        @app.get("/endless")
        async def endless_items() -> StreamingResponse:
            return stream.response(endless())

        first_chunk = asyncio.Event()
        received: list[bytes] = []
        requested = False

        async def receive() -> dict[str, Any]:
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await first_chunk.wait()
            return {"type": "http.disconnect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.body" and message["body"]:
                received.append(message["body"])
                first_chunk.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/endless",
            "raw_path": b"/endless",
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
        assert received[0].count(b"\n") == 10
        assert (source.closed, source.read < 100) == (True, True)
