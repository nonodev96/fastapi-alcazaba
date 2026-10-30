"""Every item, as they come, without pages: a stream of NDJSON (one JSON value per line), which a
client reads while the rest is still being queried, and a big table read by key, a batch at a
time, without loading it."""

import inspect
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Iterator
from itertools import islice
from typing import Any, TypeVar

import pydantic_core
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from fastapi_alcazaba.pagination.common import T

K = TypeVar("K")

_NDJSON = "application/x-ndjson"


def _line(item: object) -> bytes:
    """`item` as a line of NDJSON, as FastAPI would serialise it (by alias)."""
    return pydantic_core.to_json(item, by_alias=True) + b"\n"


def _take(
    iterator: Iterator[T],
    count: int,
) -> list[T]:
    return list(islice(iterator, count))


async def _in_threadpool(
    items: Iterable[T],
    batch_size: int,
    limit: int | None,
) -> AsyncIterator[T]:
    """Up to `limit` items of a synchronous iterable, read `batch_size` at a time in the thread
    pool: a generator that queries a database doesn't block the event loop."""
    iterator = iter(items)
    read = 0
    try:
        while limit is None or read < limit:
            count = batch_size if limit is None else min(batch_size, limit - read)
            batch = await run_in_threadpool(_take, iterator, count)
            if not batch:
                return
            read += len(batch)
            for item in batch:
                yield item
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


def _is_async(func: Callable[..., object]) -> bool:
    # A function, or an object with an async __call__.
    return inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(type(func).__call__)


class PaginationStreaming:
    """Every item, as they come, without pages: not a dependency, but a response.

        stream = PaginationStreaming(batch_size=100)

        @app.get("/events")
        async def events() -> StreamingResponse:
            async def fetch(after: int | None, limit: int) -> list[Event]:
                ...  # WHERE id > :after ORDER BY id LIMIT :limit
            return stream.response(stream.batches(fetch, key=lambda e: e.id))

    The response is NDJSON (`application/x-ndjson`): one JSON value per line, sent in chunks of
    `batch_size` lines, so that a client starts reading while the rest is being queried. A
    client that disconnects stops it. Nothing is cached: `@cache` doesn't keep streams.

    :param batch_size: lines in each chunk of the response, and rows in each query of
        `batches()`.
    :param max_items: the most items a response sends: it ends there, whatever is left. None
        for no end.
    """

    def __init__(
        self,
        *,
        batch_size: int = 100,
        max_items: int | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size starts at 1")
        if max_items is not None and max_items < 1:
            raise ValueError("max_items starts at 1")
        self.batch_size = batch_size
        self.max_items = max_items

    def response(
        self,
        items: Iterable[Any] | AsyncIterable[Any],
    ) -> StreamingResponse:
        """A response that sends `items` as NDJSON, models serialised with pydantic.

        :param items: a list, a generator, or an async iterable (an async generator,
            `batches()`). A synchronous one is read in the thread pool, `batch_size` items at a
            time. The response closes it when it's done, or at `max_items`.
        """
        return StreamingResponse(self._chunks(items), media_type=_NDJSON)

    async def _chunks(
        self,
        items: Iterable[Any] | AsyncIterable[Any],
    ) -> AsyncIterator[bytes]:
        """`batch_size` lines at a time, up to `max_items`."""
        if isinstance(items, AsyncIterable):
            iterator = aiter(items)
        else:
            iterator = aiter(_in_threadpool(items, self.batch_size, self.max_items))
        lines: list[bytes] = []
        sent = 0
        try:
            while self.max_items is None or sent < self.max_items:
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    break
                lines.append(_line(item))
                sent += 1
                if len(lines) == self.batch_size:
                    yield b"".join(lines)
                    lines = []
            if lines:
                yield b"".join(lines)
        finally:
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                await aclose()

    async def batches(
        self,
        fetch: Callable[[K | None, int], Iterable[T] | Awaitable[Iterable[T]]],
        *,
        key: Callable[[T], K],
    ) -> AsyncIterator[T]:
        """Every row of a big table, read `batch_size` rows at a time by key, without loading it
        whole: `fetch(None, batch_size)` first, then `fetch(key(last row), batch_size)`, until a
        batch comes back shorter than `batch_size`. Each query finds its batch through an index,
        as fast as the first, and rows written meanwhile don't shift the batches.

        :param fetch: `fetch(after, limit)`, synchronous (run in the thread pool) or async: up
            to `limit` rows whose key is past `after` (all of them when None), in key order
            (`WHERE id > :after ORDER BY id LIMIT :limit`).
        :param key: a row's sort key, unique (`(r.created_at, r.id)`, not `r.created_at` alone).

        Raises ValueError if `fetch` returns a batch whose last key is the one it was given: it
        isn't reading past it, and the same rows would come back for ever.
        """
        after: K | None = None
        while True:
            if _is_async(fetch):
                found = fetch(after, self.batch_size)
            else:
                found = await run_in_threadpool(fetch, after, self.batch_size)
            batch = list(await found if isinstance(found, Awaitable) else found)
            last = key(batch[-1]) if batch else None
            if after is not None and last == after:
                raise ValueError("fetch must return the rows past the key it's given")
            for row in batch:
                yield row
            if len(batch) < self.batch_size:
                return
            after = last
