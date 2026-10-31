# PaginationOffset

Slices, with the total or without counting the list: `?offset=40&limit=20`.

In an app: [the example](../../examples/pagination/offset.md). How strategies work: [Pagination](index.md#strategies). Every argument: [the API reference](../../reference/pagination.md#fastapi_alcazaba.pagination.offset.PaginationOffset).

`GET /products?offset=40&limit=20` answers `{"items": [...], "offset": 40, "limit": 20,
"has_more": true, "total": 245}`: the same as page numbers for clients that count in items
rather than pages, and can ask for any slice. When counting the list is slow (big tables,
searches, joins) or outdated as soon as it's made, leave the total out: the database is asked for
one row more than the page (`params.fetch`), which isn't returned but tells whether there's a
next one, and `total` is `null`.

```python
from fastapi_alcazaba.pagination.offset import OffsetPage, OffsetParams, PaginationOffset

offsets = PaginationOffset(default_limit=20, max_limit=100)


@app.get("/products")
async def products(params: Annotated[OffsetParams, Depends(offsets)]) -> OffsetPage[Product]:
    rows = await session.scalars(select(Product).limit(params.limit).offset(params.offset))
    return offsets.paginate(rows.all(), params, total=await count_products())


@app.get("/logs")
@cache(expire=10)
async def logs(params: Annotated[OffsetParams, Depends(offsets)]) -> OffsetPage[Line]:
    query = select(Line).order_by(Line.id.desc()).limit(params.fetch).offset(params.offset)
    rows = (await session.scalars(query)).all()
    return offsets.paginate(rows, params, queried=True)  # no count
```

- **Three ways to call `paginate`:** with `total=`, the rows are the page
  (`LIMIT params.limit OFFSET params.offset`); with `queried=True`, they're those of
  `LIMIT params.fetch OFFSET params.offset`, one more than `limit` when there's a next page;
  with neither, `paginate` slices the page from the whole list, and counts it.
- **Links:** first, previous and, while `has_more`, next; with the total, the last page too.
  First, previous and last are on the grid of `limit` from 0, where clients that start at the
  first page walk.
- **By position:** a row added or deleted before the page shifts it, so an item may be skipped
  or seen twice, and a database still walks past every row before the offset: deep pages get
  slower. For lists that grow while they're read, page by key ([`PaginationCursor`](cursor.md),
  [`PaginationKeyset`](keyset.md)).
- **Limits:** `limit` up to `max_limit` and `offset` from 0, a 422 otherwise.
