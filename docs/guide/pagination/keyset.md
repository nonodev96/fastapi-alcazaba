# PaginationKeyset

The key of the last item, in plain sight: `?after=42&limit=20`.

In an app: [the example](../../examples/pagination/keyset.md). How strategies work: [Pagination](index.md#strategies). Every argument: [the API reference](../../reference/pagination.md#fastapi_alcazaba.pagination.keyset.PaginationKeyset).

The items after the sort key of the last one seen: `GET /messages?after=42&limit=20` answers
`{"items": [...], "limit": 20, "after": "62", "has_more": true}`, and `?after=62` the next page.
The key travels as it is, neither encoded nor signed: clients read it, keep it, and may write
their own to start anywhere (the events since a date). Each page is a query through an index,
as fast as the first, and rows added or deleted meanwhile don't shift the pages. For syncing,
chats, exports and public APIs where clients drive the position:

```python
from fastapi_alcazaba.pagination.keyset import KeysetPage, KeysetParams, PaginationKeyset

messages = PaginationKeyset(sort=int, default_limit=20, max_limit=100)  # by id: ?after=42


@app.get("/messages")
@cache(expire=60, tags=["messages"])
async def list_messages(
    params: Annotated[KeysetParams, Depends(messages)],
) -> KeysetPage[Message]:
    query = select(Message).order_by(Message.id).limit(params.fetch)
    if params.after is not None:
        query = query.where(Message.id > params.after)
    rows = (await session.scalars(query)).all()
    return messages.paginate(rows, params, key=lambda m: m.id, queried=True)
```

A key of several values, such as a time and the id that breaks its ties, goes as a JSON array:

```python
events = PaginationKeyset(sort=(datetime, int))  # ?after=["2026-10-29T10:00:00Z",42]


@app.get("/events")
async def list_events(params: Annotated[KeysetParams, Depends(events)]) -> KeysetPage[Event]:
    query = select(Event).order_by(Event.at, Event.id).limit(params.fetch)
    if params.after is not None:  # (at, id) of the last event seen
        query = query.where(tuple_(Event.at, Event.id) > params.after)
    rows = (await session.scalars(query)).all()
    return events.paginate(rows, params, key=lambda e: (e.at, e.id), queried=True)
```

- **Declared types:** `sort` is the type of the key, or the types of its values in order:
  `int`, `float`, `str`, `bool`, `datetime` (with a time zone), `date`, `UUID` and `Decimal`.
  `params.after` has the key converted (a tuple for several values), and the page gives it as
  `after` takes it: a value as text (`42`, `2026-10-29T10:00:00Z`), several as a JSON array
  with datetimes and dates in ISO 8601, UUIDs and decimals as strings. A value of another type,
  another number of them or JSON that doesn't parse is a 400, and more than 1024 characters a
  422, before the endpoint runs.
- **The key** must be unique: `m.id`, or `(e.at, e.id)`, not the time alone. A key that isn't of
  the types of `sort` is a TypeError.
- **`after` and `has_more`:** `after` is the key of the page's last item, null when it's empty
  (a client that has caught up keeps its own); `has_more` says whether there's a next page. A
  client that syncs keeps `after` from the last page and asks with it later for what came since.
  The `Link` header has the first page and, while `has_more`, the next one.
- **Queried or sliced:** with `queried=True`, the rows are those of the page, queried past
  `params.after` up to `params.fetch` (the limit and one more: it tells whether there's a next
  page). Without it, `paginate` finds the page in the whole sorted list. `descending=True` for
  newest first: the next page holds the keys smaller than `after`
  (`WHERE id < :after ORDER BY id DESC`).
- **Readable and writable:** nothing stops a client from asking for any position, so sort by
  what they may see. For keys they can't change or reuse with other filters, and pages in both
  directions, use [`PaginationCursor`](cursor.md).
