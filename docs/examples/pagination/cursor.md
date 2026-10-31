# PaginationCursor

--8<-- "docs/examples/animations/pagination_cursor.svg"

An activity feed by cursor, newest first: each `next_cursor` gives the events right after the
last one seen, however many were added since, and `prev_cursor` the ones before the first. The
strategy: [PaginationCursor](../../guide/pagination/cursor.md).

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

The three newest events, and where the next page starts. There's nothing before the first page,
so `prev_cursor` is null:

```console
$ curl "http://localhost:8000/events?limit=3"
{"items":[{"id":30,"created_at":"2026-10-29T09:30:00Z","text":"Event 30"},{"id":29,"created_at":"2026-10-29T09:29:00Z","text":"Event 29"},{"id":28,"created_at":"2026-10-29T09:28:00Z","text":"Event 28"}],"limit":3,"prev_cursor":null,"next_cursor":"WyJhIixbeyJ0IjoiZGF0ZXRpbWUiLCJ2IjoiMjAyNi0xMC0yOVQwOToyODowMCswMDowMCJ9LDI4XSwiNDdERVFwajhIQlNhLV9USW1XLTVKQSIsMF0.vZy63RavbiDn-h7tl5HDPQ"}
```

A new event arrives on top meanwhile:

```console
$ curl -X POST http://localhost:8000/events -H "Content-Type: application/json" -d '{"text": "Hot news"}'
{"id":31,"created_at":"2026-10-30T19:40:40.783628Z","text":"Hot news"}
```

The next page still starts right after event 28: nothing is seen twice. It links to the pages on
both sides of it, and is cached under its own key:

```console
$ curl -i "http://localhost:8000/events?limit=3&cursor=WyJhIixbeyJ0IjoiZGF0ZXRpbWUiLCJ2IjoiMjAyNi0xMC0yOVQwOToyODowMCswMDowMCJ9LDI4XSwiNDdERVFwajhIQlNhLV9USW1XLTVKQSIsMF0.vZy63RavbiDn-h7tl5HDPQ"
HTTP/1.1 200 OK
content-type: application/json
link: </events?limit=3>; rel="first", </events?cursor=WyJiIixbeyJ0IjoiZGF0ZXRpbWUiLCJ2IjoiMjAyNi0xMC0yOVQwOToyNzowMCswMDowMCJ9LDI3XSwiNDdERVFwajhIQlNhLV9USW1XLTVKQSIsMF0.OSBP_vwXXNeYFKye264EoA&limit=3>; rel="prev", </events?cursor=WyJhIixbeyJ0IjoiZGF0ZXRpbWUiLCJ2IjoiMjAyNi0xMC0yOVQwOToyNTowMCswMDowMCJ9LDI1XSwiNDdERVFwajhIQlNhLV9USW1XLTVKQSIsMF0.GTbQ757EXZTDM_Hn0uzyHQ&limit=3>; rel="next"
etag: W/"ea02e62400d0fa258d315d4694476627"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=60

{"items":[{"id":27,"created_at":"2026-10-29T09:27:00Z","text":"Event 27"},{"id":26,"created_at":"2026-10-29T09:26:00Z","text":"Event 26"},{"id":25,"created_at":"2026-10-29T09:25:00Z","text":"Event 25"}],"limit":3,"prev_cursor":"WyJiIixbeyJ0IjoiZGF0ZXRpbWUiLCJ2IjoiMjAyNi0xMC0yOVQwOToyNzowMCswMDowMCJ9LDI3XSwiNDdERVFwajhIQlNhLV9USW1XLTVKQSIsMF0.OSBP_vwXXNeYFKye264EoA","next_cursor":"WyJhIixbeyJ0IjoiZGF0ZXRpbWUiLCJ2IjoiMjAyNi0xMC0yOVQwOToyNTowMCswMDowMCJ9LDI1XSwiNDdERVFwajhIQlNhLV9USW1XLTVKQSIsMF0.GTbQ757EXZTDM_Hn0uzyHQ"}
```

Back with `prev_cursor`: the events before 27, newest first, and this time a `prev_cursor` of its
own, since event 31 came in above them:

```console
$ curl "http://localhost:8000/events?limit=3&cursor=WyJiIixbeyJ0IjoiZGF0ZXRpbWUiLCJ2IjoiMjAyNi0xMC0yOVQwOToyNzowMCswMDowMCJ9LDI3XSwiNDdERVFwajhIQlNhLV9USW1XLTVKQSIsMF0.OSBP_vwXXNeYFKye264EoA"
{"items":[{"id":30,"created_at":"2026-10-29T09:30:00Z","text":"Event 30"},{"id":29,"created_at":"2026-10-29T09:29:00Z","text":"Event 29"},{"id":28,"created_at":"2026-10-29T09:28:00Z","text":"Event 28"}],"limit":3,"prev_cursor":"WyJiIixbeyJ0IjoiZGF0ZXRpbWUiLCJ2IjoiMjAyNi0xMC0yOVQwOTozMDowMCswMDowMCJ9LDMwXSwiNDdERVFwajhIQlNhLV9USW1XLTVKQSIsMF0.22i-EGcTS9k24Y_eMsf1-g","next_cursor":"WyJhIixbeyJ0IjoiZGF0ZXRpbWUiLCJ2IjoiMjAyNi0xMC0yOVQwOToyODowMCswMDowMCJ9LDI4XSwiNDdERVFwajhIQlNhLV9USW1XLTVKQSIsMF0.vZy63RavbiDn-h7tl5HDPQ"}
```

A cursor changed by hand doesn't match its signature:

```console
$ curl -i "http://localhost:8000/events?limit=3&cursor=WyJhIixbeyJ0IjoiZGF0ZXRpbWUiLCJ2IjoiMjAyNi0xMC0yOVQwOToyODowMCswMDowMCJ9LDI4XSwiNDdERVFwajhIQlNhLV9USW1XLTVKQSIsMF0.vZy63RavbiDn-h7tl5HDPQx"
HTTP/1.1 400 Bad Request
content-type: application/json

{"detail":"Invalid cursor"}
```

## What to look at

- **A unique sort key:** `(created_at, id)`, since two events may share a time.
- **Both ways:** `next_cursor` holds the key of the page's last event, `prev_cursor` that of its
  first. For the page before, the database reads the index the other way (oldest first, past
  the key), and `paginate` puts the rows back newest first.
- **One row more:** the database fetches `params.fetch` rows (the limit and one), which tells
  whether there's another page that way, and `paginate(..., queried=True)` keeps the page.
- **Readable, not changeable:** the cursor is base64 JSON, so clients can read the sort key, and
  signed, so they can't change it nor use it with other query parameters.
- **Pages that don't move:** a cursor's page is the same whatever is added later, so each one is
  cached under its own key; the first page is renewed every minute.

## The code

<!-- fmt: off -->
```python title="examples/pagination/cursor.py"
--8<-- "examples/pagination/cursor.py"
```
<!-- fmt: on -->
