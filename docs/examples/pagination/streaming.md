# PaginationStreaming

--8<-- "docs/examples/animations/pagination_streaming.svg"

An activity log sent whole, without pages: one JSON object per line, read from the database a
hundred events at a time by id, each batch sent as soon as it's read. A second endpoint sends the
25 newest events from a synchronous cursor, and closes it there. The strategy:
[PaginationStreaming](../../guide/pagination/streaming.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.streaming:app
```

## Try it

Every event, one per line, as they come (`-N` turns curl's buffering off):

```console
$ curl -iN http://localhost:8000/events | head -10
HTTP/1.1 200 OK
content-type: application/x-ndjson
Transfer-Encoding: chunked

{"id":1,"kind":"upload","at":"2026-10-29T09:01:00Z"}
{"id":2,"kind":"logout","at":"2026-10-29T09:02:00Z"}
{"id":3,"kind":"login","at":"2026-10-29T09:03:00Z"}
{"id":4,"kind":"upload","at":"2026-10-29T09:04:00Z"}
```

All 250 of them, from three queries of up to 100 rows; and the logins alone, from one:

```console
$ curl -s http://localhost:8000/events | wc -l
250

$ curl -s "http://localhost:8000/events?kind=login" | head -3
{"id":3,"kind":"login","at":"2026-10-29T09:03:00Z"}
{"id":6,"kind":"login","at":"2026-10-29T09:06:00Z"}
{"id":9,"kind":"login","at":"2026-10-29T09:09:00Z"}

$ curl -s "http://localhost:8000/events?kind=login" | wc -l
83
```

The newest events end at 25, however many there are:

```console
$ curl -s http://localhost:8000/events/latest | head -3
{"id":250,"kind":"upload","at":"2026-10-29T13:10:00Z"}
{"id":249,"kind":"login","at":"2026-10-29T13:09:00Z"}
{"id":248,"kind":"logout","at":"2026-10-29T13:08:00Z"}

$ curl -s http://localhost:8000/events/latest | wc -l
25
```

The parameters are checked before anything is streamed:

```console
$ curl "http://localhost:8000/events?kind=anything"
{"detail":[{"type":"enum","loc":["query","kind"],"msg":"Input should be 'login', 'upload' or 'logout'","input":"anything","ctx":{"expected":"'login', 'upload' or 'logout'"}}]}
```

## What to look at

- **No pages:** one request, one response, every event. The status and headers go out first, and
  the lines follow as they're read, in chunks of `batch_size` lines; `curl -N` shows them as they
  arrive.
- **Read by key:** `batches(fetch, key=...)` asks the database for 100 events, then for the 100
  after the last id it got, and stops at the first batch shorter than 100: three queries for 250
  events, each one through the index on id, as fast as the first. The whole log is never in
  memory.
- **Sync or async:** `fetch` here is async; the cursor of `/events/latest` is a synchronous
  generator, which the stream reads in the thread pool, ten events at a time, so a blocking
  driver doesn't stop the event loop.
- **`max_items`:** `/events/latest` ends at 25, and the cursor is closed there (its `finally`
  runs) rather than read to the end.
- **A client that disconnects** stops the stream: no more queries are made for it.
- **Not cached:** `@cache` doesn't keep streams, and each request reads the database again. Lists
  that are read often, a page at a time, are better cached by page (`PaginationCursor`).

## The code

<!-- fmt: off -->
```python title="examples/pagination/streaming.py"
--8<-- "examples/pagination/streaming.py"
```
<!-- fmt: on -->
