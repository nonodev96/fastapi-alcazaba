# PaginationStreaming

Every item as it comes, as NDJSON, without pages.

In an app: [the example](../../examples/pagination/streaming.md). How strategies work: [Pagination](index.md#strategies). Every argument: [the API reference](../../reference/pagination.md#fastapi_alcazaba.pagination.streaming.PaginationStreaming).

Every item in one response, as they come, without pages: NDJSON (`application/x-ndjson`), one
JSON value per line, which a client reads while the rest is still being queried. For exports,
for syncing everything to another system, and for scripts that would otherwise walk hundreds of
pages. It isn't a dependency: it makes the response.

```python
from fastapi.responses import StreamingResponse
from fastapi_alcazaba.pagination.streaming import PaginationStreaming

stream = PaginationStreaming(batch_size=100)


@app.get("/events")
async def events() -> StreamingResponse:
    async def fetch(after: int | None, limit: int) -> list[Event]:
        query = select(Event).order_by(Event.id).limit(limit)
        if after is not None:
            query = query.where(Event.id > after)
        return list((await session.scalars(query)).all())

    return stream.response(stream.batches(fetch, key=lambda e: e.id))
```

`GET /events` answers one event per line, `{"id":1,...}`, `{"id":2,...}`..., each hundred sent
as soon as it's read, before the next hundred are queried, until the last event.

- **`response(items)`** takes a list, a generator or an async iterable, and sends it in chunks of
  `batch_size` lines, models serialised with pydantic as FastAPI would. A synchronous generator
  is read in the thread pool, `batch_size` items at a time, so a blocking driver doesn't stop
  the event loop.
- **`batches(fetch, key=...)`** reads a big table without loading it: `fetch(None, batch_size)`,
  then `fetch(key(last row), batch_size)`, until a batch comes back shorter than `batch_size`.
  Each query finds its batch through an index, as fast as the first. `fetch` can be sync or
  async, and `key` must be unique (`(e.created_at, e.id)` rather than `e.created_at`). It
  yields the rows one by one, ready for `response()`.
- **`max_items`** ends the response there, whatever is left (None, the default, for no end).
  The source is closed then, or at its end: a generator's `finally` runs.
- **A client that disconnects** stops the stream: nothing more is read from the source, and no
  more queries are made.
- **Not cached:** `@cache` doesn't keep streams, so each request reads the database again; don't
  put it on these endpoints.
- **Its limits:** the status and headers go out before the first item, so an error halfway
  through can only cut the stream short; clients should check that they got what they expected
  (a count in another endpoint, or the last id). There's no resuming in the middle: a client
  that needs it pages by cursor instead.
