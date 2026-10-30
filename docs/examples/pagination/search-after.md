# PaginationSearchAfter

--8<-- "docs/examples/animations/pagination_search_after.svg"

A shop's activity log, oldest first, read after the sort values of the last event seen: the
time and the id, as a JSON array that clients read and may write themselves. The strategy:
[PaginationSearchAfter](../../guide/pagination/search-after.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.search_after:app
```

Nothing to configure: the values aren't signed.

## Try it

The first three events, and in `search_after` the time and id of the last one, as the event
shows them:

```console
$ curl -i "http://localhost:8000/events"
HTTP/1.1 200 OK
content-type: application/json
link: </events?limit=3>; rel="first", </events?search_after=%5B%222026-10-29T09%3A58%3A30Z%22%2C3%5D&limit=3>; rel="next"
etag: W/"8258945f8d0c72e3311e50b5c99e128a"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=60
vary: Authorization, Cookie

{"items":[{"id":1,"at":"2026-10-29T09:58:00Z","action":"login"},{"id":2,"at":"2026-10-29T09:58:30Z","action":"cart.updated"},{"id":3,"at":"2026-10-29T09:58:30Z","action":"cart.updated"}],"limit":3,"search_after":["2026-10-29T09:58:30Z",3]}
```

The next page sends that array back, as the `next` link does (percent-encoded in the URL):

```console
$ curl "http://localhost:8000/events?search_after=%5B%222026-10-29T09%3A58%3A30Z%22%2C3%5D&limit=3"
{"items":[{"id":4,"at":"2026-10-29T09:59:15Z","action":"order.created"},{"id":5,"at":"2026-10-29T10:00:00Z","action":"payment.authorized"},{"id":6,"at":"2026-10-29T10:00:00Z","action":"order.confirmed"}],"limit":3,"search_after":["2026-10-29T10:00:00Z",6]}
```

A client can as well write values of its own: from 10:00 on, with an id smaller than any, so
that no event of 10:00 is left out. `curl -G --data-urlencode` encodes the array:

```console
$ curl -G "http://localhost:8000/events" --data-urlencode 'search_after=["2026-10-29T10:00:00Z",0]'
{"items":[{"id":5,"at":"2026-10-29T10:00:00Z","action":"payment.authorized"},{"id":6,"at":"2026-10-29T10:00:00Z","action":"order.confirmed"},{"id":7,"at":"2026-10-29T10:00:00Z","action":"email.sent"}],"limit":3,"search_after":["2026-10-29T10:00:00Z",7]}

$ curl -siG "http://localhost:8000/events" --data-urlencode 'search_after=["2026-10-29T10:00:00Z",0]' | grep cache-status
cache-status: Alcazaba; hit; ttl=60
```

On the last page `search_after` is null, and the only link is to the first page:

```console
$ curl -G "http://localhost:8000/events" --data-urlencode 'search_after=["2026-10-29T10:01:40Z",8]'
{"items":[{"id":9,"at":"2026-10-29T10:03:20Z","action":"email.sent"},{"id":10,"at":"2026-10-29T10:05:00Z","action":"logout"}],"limit":3,"search_after":null}
```

Values that aren't of the declared types are refused before anything is queried: a time
without its zone, an id as a string, one value of two:

```console
$ curl -iG "http://localhost:8000/events" --data-urlencode 'search_after=["2026-10-29T10:00:00",0]'
HTTP/1.1 400 Bad Request
content-type: application/json

{"detail":"Invalid search_after"}

$ curl -G "http://localhost:8000/events" --data-urlencode 'search_after=["2026-10-29T10:00:00Z","0"]'
{"detail":"Invalid search_after"}

$ curl -G "http://localhost:8000/events" --data-urlencode 'search_after=["2026-10-29T10:00:00Z"]'
{"detail":"Invalid search_after"}
```

## What to look at

- **Declared types:** `PaginationSearchAfter(sort=(datetime, int))`. The time comes back as a
  `datetime` with its zone, and the id as an `int`, ready for the query's
  `WHERE (at, id) > :search_after`.
- **A unique key:** the time and the id, since three events share 10:00; the id after the time
  keeps them in one order, and none is skipped between pages.
- **Plain JSON, not signed:** clients read the values and may start where they like, so sort by
  what they may see. For a position they can't change, `PaginationCursor` seals the same key.
- **One row more:** the database fetches `params.fetch` rows (the limit and one), which tells
  whether there's a next page.

## The code

<!-- fmt: off -->
```python title="examples/pagination/search_after.py"
--8<-- "examples/pagination/search_after.py"
```
<!-- fmt: on -->
