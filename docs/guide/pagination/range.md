# PaginationRange

Ranges of items, with `Content-Range`: `?start=0&end=24`.

In an app: [the example](../../examples/pagination/range.md). How strategies work: [Pagination](index.md#strategies). Every argument: [the API reference](../../reference/pagination.md#fastapi_alcazaba.pagination.ranges.PaginationRange).

Ranges of items by the indexes of the first and the last, from 0 and both included:
`GET /contacts?start=0&end=24` answers `{"items": [...], "start": 0, "end": 24, "total": 319,
"limit": 25}` with `Content-Range: items 0-24/319`. For data grids and tables that ask for the
rows they show, and read from the header which ones they got and how many there are:

```python
from fastapi_alcazaba.pagination.ranges import PaginationRange, RangePage, RangeParams

ranges = PaginationRange(default_items=25, max_items=100)


@app.get("/contacts")
@cache(expire=60, tags=["contacts"])
async def contacts(params: Annotated[RangeParams, Depends(ranges)]) -> RangePage[Contact]:
    query = select(Contact).order_by(Contact.name).limit(params.limit).offset(params.offset)
    rows = (await session.scalars(query)).all()
    return ranges.paginate(rows, params, total=await count_contacts())
```

- **The range returned:** `end` is the index of the last item in the response, which is short of
  the one asked for at the end of the list; `null`, and `Content-Range: items */319`, when there
  are none. `limit` is the size asked for, which the links keep.
- **From a database:** query the range with `params.offset` and `params.limit`, and count:
  `ranges.paginate(rows, params, total=count)`. Without `total`, `paginate` slices the range from
  the whole list.
- **Errors:** without `end`, a range holds `default_items`. One whose `end` is before its `start`,
  or that holds more than `max_items` items, is a 400 before the endpoint runs; one that starts
  past the last item of a list that isn't empty, a 416 with `Content-Range: items */<total>`.
- **Links:** first, previous, next and last ranges of the same size, the first and last on the
  grid of that size from 0. Pages served from the cache get their `Content-Range` too.
- **Its cost:** like page numbers, a count on every range, and pages that shift when rows are
  added or deleted before them.
