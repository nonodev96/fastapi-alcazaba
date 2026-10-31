# Pagination

In an app: [the examples](../../examples/pagination/index.md). Every class and function: [the API reference](../../reference/pagination.md).

## Strategies

Seven strategies, with nothing but FastAPI and pydantic. Each is configured once and used as a
dependency, which reads and checks its query parameters; the endpoint gets them as an argument, so
`@cache` keeps each page under a key of its own:

| Strategy | Query parameters | For |
| --- | --- | --- |
| `PaginationPageNumber` | `page`, `page_size` | Numbered pages, with the total |
| `PaginationOffset` | `offset`, `limit` | Slices, with the total or without counting the list |
| `PaginationRange` | `start`, `end` | Ranges of items, with `Content-Range` |
| `PaginationCursor` | `cursor`, `limit` | Opaque, signed cursors, both ways, bound to the query: feeds, infinite scroll |
| `PaginationKeyset` | `after`, `limit` | The key of the last item in plain sight (an id, a time, several values): syncing, public APIs |
| `PaginationScroll` | `scroll_id`, `batch_size` | A snapshot kept in the backend, read in batches |
| `PaginationStreaming` | none | Every item as it comes, as NDJSON |

Which one: numbered pages for people who jump to page 7, `PaginationOffset` for slices of a
list that doesn't change much (without its total when counting it is slow),
`PaginationCursor` for feeds and lists that change while they're read, `PaginationKeyset` when
clients sync or write their own starting point (`?after=2026-10-29T10:00:00Z`), and a scroll or
a stream to read everything: exports, reindexing.

```python
from fastapi_alcazaba.pagination.page_number import (
    PageNumberPage,
    PageNumberParams,
    PaginationPageNumber,
)

pages = PaginationPageNumber(default_page_size=20, max_page_size=100)


@app.get("/products")
@cache(expire=60, tags=["products"])
async def products(
    params: Annotated[PageNumberParams, Depends(pages)],
) -> PageNumberPage[Product]:
    return pages.paginate(await load_products(), params)
```

- **By position or by key:** page numbers, offsets and ranges count items, so a row added or
  deleted meanwhile shifts the pages (an item skipped or seen twice), and a database walks past
  every row before the page. Cursors and keysets point at the last item instead: the pages don't shift, and an index finds any of them as fast as the first.
- **Links:** the response links to the pages around it in the `Link` header
  ([RFC 8288](https://www.rfc-editor.org/rfc/rfc8288)), as GitHub's API does:
  `</products?page=3&page_size=20>; rel="next"`. The links keep the request's other query
  parameters and leave out the host, so they don't depend on the `Host` header nor on a proxy in
  front. Pages served from the cache get them too, made for the request they answer.
- **From a database:** `paginate` takes the whole list and finds the page in it, or the rows the
  database returned for the page: with their `total` for the strategies that count, or
  `queried=True` for the others, which fetch one row more (`params.fetch`) to tell whether
  there's a next page.
- **Limits:** each strategy has its own maximum (100 items by default), so that nobody asks for a
  million rows at once: anything beyond, or malformed, is a 422 before the endpoint runs. They're
  in the OpenAPI schema too.
- **Cached:** every page, size and filter is one entry: tag them (`tags=["products"]`) to drop
  them all when the data changes. For short lists, caching the whole list with `get_or_set` and
  slicing every page from it keeps a single entry. The
  [pagination example](https://github.com/nonodev96/fastapi-alcazaba/blob/main/examples/pagination/main.py)
  does it three ways, with the database session left out of the key (`exclude_params`).

Each strategy has its page:

- [`PaginationPageNumber`](page-number.md): numbered pages, with the total: `?page=2&page_size=20`.
- [`PaginationOffset`](offset.md): slices, with the total or without it: `?offset=40&limit=20`.
- [`PaginationRange`](range.md): ranges of items, with `Content-Range`: `?start=0&end=24`.
- [`PaginationCursor`](cursor.md): opaque, signed cursors, both ways: `?cursor=...&limit=20`.
- [`PaginationKeyset`](keyset.md): the key of the last item, readable: `?after=42&limit=20`.
- [`PaginationScroll`](scroll.md): a snapshot kept in the backend, read in batches: `?scroll_id=...`.
- [`PaginationStreaming`](streaming.md): every item as it comes, as NDJSON, without pages.

And for the queries: [With SQLAlchemy](sqlalchemy.md), `page_query` and `count_query` from the
parameters of any strategy.
