# PaginationScroll

A snapshot kept in the backend, read in batches: `?scroll_id=...`.

In an app: [the example](../../examples/pagination/scroll.md). How strategies work: [Pagination](index.md#strategies). Every argument: [the API reference](../../reference/pagination.md#fastapi_alcazaba.pagination.scroll.PaginationScroll).

A scroll runs the query once and keeps what it found as a snapshot in the cache's backend, which
the client reads in batches: what was there when the scroll started, whatever is written
meanwhile. For exports, reports and jobs that read a whole result set while it changes, and for
queries too costly to run once per page:

```python
from fastapi_alcazaba.pagination.scroll import PaginationScroll, ScrollPage, ScrollParams

scroll = PaginationScroll(Order, secret=settings.scroll_secret)  # secrets.token_urlsafe(32)


@app.get("/orders/export")
async def export(params: Annotated[ScrollParams, Depends(scroll)]) -> ScrollPage[Order]:
    if params.scroll_id is None:  # a new scroll: query once, one row more than it keeps
        query = select(Order).where(Order.status == "paid").order_by(Order.id)
        rows = await session.scalars(query.limit(scroll.max_items + 1))
        return await scroll.start(rows.all(), params)
    return await scroll.next(params)


@app.delete("/orders/export", status_code=204)
async def stop(scroll_id: str) -> None:
    await scroll.clear(scroll_id)
```

`GET /orders/export?batch_size=100` answers the first batch,
`{"items": [...], "scroll_id": "WyJsR3Nq...", "total": 2450}`, and `?scroll_id=<scroll_id>` the
next one; `scroll_id` is null after the last. The `Link` header has the next batch.

- **A snapshot:** `start` writes the items into one entry of the backend, as JSON through their
  type (`PaginationScroll(Order, ...)`), and `next` reads them back as such. Rows added, changed
  or deleted meanwhile don't show, and no item is skipped or seen twice.
- **Kept alive while it's read:** the snapshot lasts `keep_alive` seconds (60 by default) after
  each batch; a `scroll_id` whose snapshot has expired is a 404, and the client starts again.
  `clear` deletes it as soon as the client is done. The cache's own `clear()` leaves snapshots
  alone, and with a TieredBackend they're in the shared tier only.
- **Retries are safe:** a `scroll_id` holds the snapshot's id and where the next batch starts,
  signed with HMAC-SHA256: the same request gets the same batch, and one made up or changed is a
  400 before the endpoint runs. Each request may ask for its own `batch_size`, up to
  `max_batch_size` (1000 by default; a 422 beyond).
- **Who may read it:** a `scroll_id` reads the rest of the snapshot without the endpoint's query
  running again, so whoever has one gets the results it was given for. When they depend on who
  asks, pass `owner=str(user.id)` to `start`, `next` and `clear`: a `scroll_id` is then only good
  for that user.
- **Sealed:** the snapshot is sealed with the cache's `signing_key`, or `encryption_key` when it
  holds personal data, and one that someone planted in the backend is ignored.
- **Its limits:** each batch reads the whole snapshot from the backend and writes it back to keep
  it alive, and each start keeps a new one, so `start` refuses more than `max_items` items (10,000
  by default; query one more than that and it raises ValueError) and a
  rate limit on the endpoint keeps clients from starting scrolls without end.
  The snapshot is one entry: mind the backend's limit on the size of an entry (1 MB in Memcached,
  400 KB in DynamoDB). A backend that fails is a 503, and the client retries. Don't put `@cache`
  on a scroll: each start must keep a snapshot of its own. To read a big table without keeping
  it anywhere, `PaginationStreaming` sends every row as it comes.
