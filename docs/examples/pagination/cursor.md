# PaginationCursor

--8<-- "docs/examples/animations/pagination_cursor.svg"

An activity feed by cursor, newest first: each `next_cursor` gives the events right after the
last one seen, however many were added since. The strategy:
[PaginationCursor](../../guide/pagination/cursor.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.cursor:app
```

Set `CURSOR_SECRET` (`secrets.token_urlsafe(32)`), the same in every process. Without it each
start makes up a throwaway one, and the cursors stop working when the app restarts.

## Try it

The three newest events, and where the next page starts:

```console
$ curl "http://localhost:8000/events?limit=3"
{"items":[{"id":30,"created_at":"2026-10-29T09:30:00Z","text":"Event 30"},{"id":29,"created_at":"2026-10-29T09:29:00Z","text":"Event 29"},{"id":28,"created_at":"2026-10-29T09:28:00Z","text":"Event 28"}],"limit":3,"next_cursor":"W3sidCI6ImRhdGV0aW1lIiwidiI6IjIwMjYtMTAtMjlUMDk6Mjg6MDArMDA6MDAifSwyOF0.DdxtJ1jx3H6AjMK8W0C6mg"}
```

A new event arrives on top meanwhile:

```console
$ curl -X POST http://localhost:8000/events -H "Content-Type: application/json" -d '{"text": "Hot news"}'
{"id":31,"created_at":"2026-10-29T15:59:05.809457Z","text":"Hot news"}
```

The next page still starts right after event 28: nothing is seen twice.

```console
$ curl -i "http://localhost:8000/events?limit=3&cursor=W3sidCI6ImRhdGV0aW1lIiwidiI6IjIwMjYtMTAtMjlUMDk6Mjg6MDArMDA6MDAifSwyOF0.DdxtJ1jx3H6AjMK8W0C6mg"
HTTP/1.1 200 OK
content-type: application/json
link: </events?limit=3>; rel="first", </events?cursor=W3sidCI6ImRhdGV0aW1lIiwidiI6IjIwMjYtMTAtMjlUMDk6MjU6MDArMDA6MDAifSwyNV0.o1vH1zVOWgrtWAI-whHW9w&limit=3>; rel="next"
etag: W/"d2845929a8686eed133e6e69e12ab48d"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=60

{"items":[{"id":27,"created_at":"2026-10-29T09:27:00Z","text":"Event 27"},{"id":26,"created_at":"2026-10-29T09:26:00Z","text":"Event 26"},{"id":25,"created_at":"2026-10-29T09:25:00Z","text":"Event 25"}],"limit":3,"next_cursor":"W3sidCI6ImRhdGV0aW1lIiwidiI6IjIwMjYtMTAtMjlUMDk6MjU6MDArMDA6MDAifSwyNV0.o1vH1zVOWgrtWAI-whHW9w"}
```

A cursor changed by hand doesn't match its signature:

```console
$ curl -i "http://localhost:8000/events?limit=3&cursor=W3sidCI6ImRhdGV0aW1lIiwidiI6IjIwMjYtMTAtMjlUMDk6Mjg6MDArMDA6MDAifSwyOF0.DdxtJ1jx3H6AjMK8W0C6mgx"
HTTP/1.1 400 Bad Request

{"detail":"Invalid cursor"}
```

## What to look at

- **A unique sort key:** `(created_at, id)`, since two events may share a time.
- **One row more:** the database fetches `params.fetch` rows (the limit and one), which tells
  whether there's a next page, and `paginate(..., queried=True)` keeps the page.
- **Readable, not changeable:** the cursor is base64 JSON, so clients can read the sort key, and
  signed, so they can't change it.
- **Pages that don't move:** a cursor's page is the same whatever is added later, so each one is
  cached under its own key; the first page is renewed every minute.

## The code

<!-- fmt: off -->
```python title="examples/pagination/cursor.py"
--8<-- "examples/pagination/cursor.py"
```
<!-- fmt: on -->
