# Pagination

In an app: [the examples](../../examples/pagination/index.md). Every class and function: [the API reference](../../reference/pagination.md).

## Strategies

--8<-- "README.md:pagination"

Each strategy has its page:

- [`PaginationPageNumber`](page-number.md): numbered pages, with the total: `?page=2&page_size=20`.
- [`PaginationLimitOffset`](limit-offset.md): slices, with the total: `?limit=20&offset=40`.
- [`PaginationOffset`](offset.md): slices without counting the whole list: `?offset=40&limit=20`.
- [`PaginationRange`](range.md): ranges of items, with `Content-Range`: `?start=0&end=24`.
- [`PaginationCursor`](cursor.md): an opaque, signed cursor: `?limit=20&cursor=...`.
- [`PaginationKeyset`](keyset.md): keyset pagination, both ways: `?after=...` or `?before=...`.
- [`PaginationIdBased`](id-based.md): the id of the last item seen: `?last_id=42&limit=20`.
- [`PaginationTimeBased`](time-based.md): before or after a time: `?timestamp=2026-10-29T10:00:00Z&limit=20`.
- [`PaginationSearchAfter`](search-after.md): the sort values of the last item: `?search_after=["...",42]&limit=20`.
- [`PaginationToken`](token.md): an opaque token, bound to the query, that expires: `?page_token=...&page_size=20`.
- [`PaginationScroll`](scroll.md): a snapshot kept in the backend, read in batches: `?scroll_id=...`.
- [`PaginationStreaming`](streaming.md): every item as it comes, as NDJSON, without pages.
