# PaginationTimeBased

--8<-- "docs/examples/animations/pagination_time_based.svg"

A cooperative's news feed by time: `/posts` newest first for readers, `/posts/since` oldest
first for a client that syncs. A page never splits the posts that share a time, so the next one,
which starts past it, skips none. The strategy:
[PaginationTimeBased](../../guide/pagination/time-based.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.time_based:app
```

Nothing to configure: the timestamps aren't signed.

## Try it

The three newest posts, and in `next_timestamp` the time of the last one:

```console
$ curl -i "http://localhost:8000/posts"
HTTP/1.1 200 OK
content-type: application/json
link: </posts?limit=3>; rel="first", </posts?timestamp=2026-10-29T10%3A00%3A00Z&limit=3>; rel="next"
etag: W/"fae4dd2eed51634151ef4bf0ef13c78a"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=60

{"items":[{"title":"The bottling line is back","id":9,"posted_at":"2026-10-29T10:20:00Z"},{"title":"Recipe: salmorejo","id":8,"posted_at":"2026-10-29T10:00:00Z"},{"title":"Tasting at the cooperative","id":7,"posted_at":"2026-10-29T10:00:00Z"}],"limit":3,"next_timestamp":"2026-10-29T10:00:00Z"}
```

The posts before 10:00. The database returned four (the limit and one more), but the last two
and the extra one share 09:00: they go together on the next page, and this one holds one post:

```console
$ curl -i "http://localhost:8000/posts?timestamp=2026-10-29T10%3A00%3A00Z&limit=3"
HTTP/1.1 200 OK
content-type: application/json
link: </posts?limit=3>; rel="first", </posts?timestamp=2026-10-29T09%3A30%3A00Z&limit=3>; rel="next"
etag: W/"966c6d24b7da1aec29fe05acdf5cfc6f"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=60

{"items":[{"title":"The mill opens on Saturday","id":6,"posted_at":"2026-10-29T09:30:00Z"}],"limit":3,"next_timestamp":"2026-10-29T09:30:00Z"}

$ curl "http://localhost:8000/posts?timestamp=2026-10-29T09%3A30%3A00Z&limit=3"
{"items":[{"title":"Price list: Arbequina","id":5,"posted_at":"2026-10-29T09:00:00Z"},{"title":"Price list: Hojiblanca","id":4,"posted_at":"2026-10-29T09:00:00Z"},{"title":"Price list: Picual","id":3,"posted_at":"2026-10-29T09:00:00Z"}],"limit":3,"next_timestamp":"2026-10-29T09:00:00Z"}
```

With pages of two, the three price lists can't fit in one, and splitting them would skip some:
`paginate` raises ValueError, which the example answers with a 400:

```console
$ curl -i "http://localhost:8000/posts?timestamp=2026-10-29T09:30:00Z&limit=2"
HTTP/1.1 400 Bad Request
content-type: application/json

{"detail":"More than 2 posts share a time: ask for more"}
```

A timestamp needs its time zone; any zone will do, and the same instant is the same cache entry:

```console
$ curl "http://localhost:8000/posts?timestamp=2026-10-29T09:30:00"
{"detail":[{"type":"timezone_aware","loc":["query","timestamp"],"msg":"Input should have timezone info","input":"2026-10-29T09:30:00"}]}

$ curl -si "http://localhost:8000/posts?timestamp=2026-10-29T11:00:00%2B01:00" | grep cache-status
cache-status: Alcazaba; hit; ttl=56
```

Syncing, oldest first: the posts since 08:15. The first page stops before the three of 09:00:

```console
$ curl -i "http://localhost:8000/posts/since?timestamp=2026-10-29T08:15:00Z"
HTTP/1.1 200 OK
content-type: application/json
link: </posts/since?limit=3>; rel="first", </posts/since?timestamp=2026-10-29T08%3A40%3A00Z&limit=3>; rel="next"
etag: W/"81b04e0669a930cbf1b7e03f6dbe168f"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=60

{"items":[{"title":"Frost warning for the groves","id":2,"posted_at":"2026-10-29T08:40:00Z"}],"limit":3,"next_timestamp":"2026-10-29T08:40:00Z"}
```

At the end `next_timestamp` is null: the client keeps the time of the last post it got, and asks
again later. A new post drops the cached pages (tag `posts`), and the next sync gets it:

```console
$ curl "http://localhost:8000/posts/since?timestamp=2026-10-29T10:20:00Z"
{"items":[],"limit":3,"next_timestamp":null}

$ curl -X POST http://localhost:8000/posts -H "Content-Type: application/json" -d '{"title": "Open day at the mill"}'
{"title":"Open day at the mill","id":10,"posted_at":"2026-10-30T09:21:45.133838Z"}

$ curl "http://localhost:8000/posts/since?timestamp=2026-10-29T10:20:00Z"
{"items":[{"title":"Open day at the mill","id":10,"posted_at":"2026-10-30T09:21:45.133838Z"}],"limit":3,"next_timestamp":null}
```

## What to look at

- **Two directions, two instances:** `PaginationTimeBased("older")` for the feed
  (`WHERE posted_at < :timestamp ORDER BY posted_at DESC`) and `PaginationTimeBased("newer")`
  for syncing (`WHERE posted_at > :timestamp ORDER BY posted_at`).
- **Ties:** the next page starts strictly past the last time, so a page never ends halfway
  through the posts of one time. With queried rows, the extra row shows when they go on, and the
  page leaves them all to the next one.
- **More than a page of one time:** `paginate` raises ValueError rather than skip posts. If that
  can happen with your data, paginate by a unique key with `PaginationKeyset` (the time and the
  id).
- **Plain timestamps:** ISO 8601 with a time zone, readable and writable by clients; the page's
  links put them in UTC, as the JSON does.

## The code

<!-- fmt: off -->
```python title="examples/pagination/time_based.py"
--8<-- "examples/pagination/time_based.py"
```
<!-- fmt: on -->
