"""Pagination strategies, each a dependency that reads and checks its query parameters, with a
`paginate` that makes the page: each page cached under its own key, and linked to the pages
around it (RFC 8288). Each strategy is in its own module, with its parameters and its page:

    from fastapi_alcazaba.pagination.cursor import CursorPage, CursorParams, PaginationCursor

- By position: `page_number` (page, page_size), `limit_offset` (limit, offset, with the total),
  `offset` (offset, limit, without it) and `ranges` (start, end, with Content-Range).
- By sort key: `cursor` (an opaque cursor), `keyset` (after and before), `id_based` (last_id),
  `time_based` (timestamp) and `search_after` (the sort values of the last item).
- Other ways: `page_token` (page_token, bound to the query), `scroll` (a snapshot kept in the
  backend) and `streaming` (every item, as they come).

What they share is here.
"""

from fastapi_alcazaba.pagination.common import LinkedPage, PageLinks, PaginationParams
from fastapi_alcazaba.pagination.values import CursorValue, Position

__all__ = [
    "CursorValue",
    "LinkedPage",
    "PageLinks",
    "PaginationParams",
    "Position",
]
