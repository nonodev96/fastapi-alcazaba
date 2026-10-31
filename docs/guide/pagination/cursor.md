# PaginationCursor

Opaque, signed cursors, both ways, bound to the query: `?cursor=...&limit=20`.

In an app: [the example](../../examples/pagination/cursor.md). How strategies work: [Pagination](index.md#strategies). Every argument: [the API reference](../../reference/pagination.md#fastapi_alcazaba.pagination.cursor.PaginationCursor).

A cursor points at the item at the edge of a page by its sort key, not by its position: rows
added or deleted meanwhile don't shift the pages (no item skipped or seen twice), and a database
finds any page through an index, from either side, as fast as the first. Cursors are the way to
the next and previous pages and nothing else: opaque, signed, bound to the query they were
issued for and, with a `ttl`, short-lived. For feeds, infinite scroll, lists with "Previous" and
"Next", and public APIs whose pagination may change behind the cursor without breaking anybody:

```python
from fastapi_alcazaba.pagination.cursor import CursorPage, CursorParams, PaginationCursor

cursors = PaginationCursor(settings.cursor_secret)  # secrets.token_urlsafe(32)


@app.get("/events")
@cache(expire=60)
async def events(
    params: Annotated[CursorParams, Depends(cursors)],
    kind: Kind | None = None,
) -> CursorPage[Event]:
    key = tuple_(Event.created_at, Event.id)
    if params.before is not None:  # the page before: the nearest rows first
        query = select(Event).where(key < params.before)
        query = query.order_by(Event.created_at.desc(), Event.id.desc())
    else:
        query = select(Event).order_by(Event.created_at, Event.id)
        if params.after is not None:
            query = query.where(key > params.after)
    if kind is not None:
        query = query.where(Event.kind == kind)
    rows = (await session.scalars(query.limit(params.fetch))).all()
    return cursors.paginate(rows, params, key=lambda e: (e.created_at, e.id), queried=True)
```

`GET /events?kind=login&limit=20` answers `{"items": [...], "limit": 20, "prev_cursor": null,
"next_cursor": "WyJhIixb..."}`, and `?kind=login&cursor=<next_cursor>` the next page:
`next_cursor` is null on the last page, `prev_cursor` on the first. The `Link` header has the
first page and, when there's one, the previous and the next.

- **The sort key** (`key`) must be unique: `(created_at, id)`, not `created_at` alone, which two
  items may share. Its values can be strings, integers (up to 64 bits), floats, booleans,
  datetimes, dates, UUIDs and decimals, and `params.after` and `params.before` have them back as
  such: one of them is set, by the cursor's direction, neither on the first page.
- **Queried or sliced:** with `queried=True`, the rows are those the database returned, up to
  `params.fetch` (the limit and one more, which tells whether there's another page that way):
  past `params.after` in order, or with `params.before`, before it in the reverse order, nearest
  first, which `paginate` puts back in order. The other way there's always a page, the one the
  cursor came from. Without it, `paginate` finds the page in the whole sorted list.
  `descending=True` for newest first, where `after` reads `key < :after` and `before`
  `key > :before ORDER BY key`.
- **What a cursor holds:** its direction, the sort key, a digest of the request's other query
  parameters (all but `cursor` and `limit`, in any order) and, with a `ttl`, when it was issued,
  signed with HMAC-SHA256 (`secret` is required; a list rotates secrets). A cursor that a client
  made or changed, that comes with other query parameters than its own (the next page of
  `kind=login` asked with `kind=logout`), or older than `ttl` is a 400 before the endpoint runs;
  one longer than 1024 characters or malformed is a 422. Cursors are base64, not encrypted:
  clients can read the sort key, so sort by what they may see.
- **Cached apart:** the query's digest is part of `CursorParams`, so `@cache` keeps the pages of
  each query apart, even for parameters the endpoint doesn't read. With a `ttl`, a page served
  from the cache hands out the cursors issued when it was stored: keep `expire` well under `ttl`.
- **Links of your own:** `cursors.encode(position, before=False, query=[...])` makes the cursor
  of the page after (or before) any position, such as the events after a date.
- **Its limits:** no total and no page numbers, only the first, previous and next pages. Going
  back from a page that didn't start where the first page's walk would have, the first page can
  be shorter than the limit. For a key clients read and write themselves, use
  [`PaginationKeyset`](keyset.md).
