"""Pagination strategies, each a dependency that reads and checks its query parameters, with a
`paginate` that makes the page: each page cached under its own key, and linked to the pages
around it (RFC 8288). Each strategy is in its own module, with its parameters and its page:

    from fastapi_alcazaba.pagination.cursor import CursorPage, CursorParams, PaginationCursor

- By position: `page_number` (page, page_size, with the total), `offset` (offset, limit, with
  the total or without it) and `ranges` (start, end, with Content-Range).
- By sort key: `cursor` (opaque and signed, both directions, bound to the query) and `keyset`
  (the key of the last item in plain sight: an id, a time, or several values).
- Other ways: `scroll` (a snapshot kept in the backend) and `streaming` (every item, as they
  come).

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
