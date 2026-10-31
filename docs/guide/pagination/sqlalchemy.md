# With SQLAlchemy

The queries of the strategies, written for SQLAlchemy 2: `pip install "fastapi-alcazaba[sqlalchemy]"`.

How strategies work: [Pagination](index.md#strategies). Every argument: [the API reference](../../reference/pagination.md#fastapi_alcazaba.pagination.sqlalchemy.page_query).

The JSON of a page is the easy part; the query is where pagination goes wrong: a sort key that
isn't unique skips rows, a comparison column by column instead of as a row value walks past the
index, a previous page read in the wrong order comes back reversed, a forgotten extra row loses
`has_more`. `page_query` writes it from the parameters the dependency gave:

```python
from sqlalchemy import select

from fastapi_alcazaba.pagination.cursor import CursorPage, CursorParams, PaginationCursor
from fastapi_alcazaba.pagination.sqlalchemy import count_query, page_query, sort_key

cursors = PaginationCursor(settings.cursor_secret)
BY = (Event.created_at, Event.id)  # unique as a whole


@app.get("/events")
async def events(
    params: Annotated[CursorParams, Depends(cursors)],
    session: SessionDep,
    kind: Kind | None = None,
) -> CursorPage[EventOut]:
    statement = select(Event)
    if kind is not None:
        statement = statement.where(Event.kind == kind)
    query = page_query(statement, params, by=BY, descending=True)
    rows = (await session.scalars(query)).all()
    return cursors.paginate(rows, params, key=sort_key(*BY), descending=True, queried=True)
```

By position, with the total:

```python
@app.get("/products")
async def products(
    params: Annotated[PageNumberParams, Depends(pages)],
    session: SessionDep,
) -> PageNumberPage[ProductOut]:
    statement = select(Product).where(Product.active)
    total = await session.scalar(count_query(statement))
    rows = (await session.scalars(page_query(statement, params, by=(Product.id,)))).all()
    return pages.paginate(rows, params, total=total)
```

- **By key** (`CursorParams`, `KeysetParams`): `by` is the sort key, and the statement has no
  `ORDER BY` of its own. `page_query` adds `WHERE (created_at, id) > (:a, :b)` (`<` newest first),
  the order of `by`, and `LIMIT` one more than the page. For the page before a cursor, it reads
  the other way, nearest first, as `paginate(..., queried=True)` expects. The comparison is a row
  value, which PostgreSQL, MySQL 8, MariaDB and SQLite 3.15+ answer from an index on the columns
  of `by`, in that order.
- **By position** (`PageNumberParams`, `OffsetParams`, `RangeParams`): `OFFSET` and `LIMIT`, in
  the statement's order or that of `by`. With `OffsetParams` it fetches one row more, for
  `queried=True`; with `total=` the extra row is left out of the page.
- **`count_query(statement)`:** `SELECT count(*)` of the statement, without its order, limit or
  offset.
- **`sort_key(*columns)`:** the `key` of `paginate` for those columns, read from each row by
  name: `(event.created_at, event.id)`, or the value alone for one column.
- **Positions that don't fit:** a cursor whose key has another number of values than `by` (one
  of another endpoint signed with the same secret) is a 400, like any invalid cursor.
