"""Pagination by page number, by limit and offset, and by signed cursor: each page cached
under its own key, and linked to the pages around it (RFC 8288)."""

from fastapi_alcazaba.pagination.paginators import (
    CursorPage,
    CursorParams,
    CursorValue,
    FastAPIPagination,
    FastAPIPaginationCursor,
    FastAPIPaginationLimitOffset,
    LimitOffsetPage,
    LimitOffsetParams,
    Page,
    PageLinks,
    PageParams,
    Position,
)

__all__ = [
    "CursorPage",
    "CursorParams",
    "CursorValue",
    "FastAPIPagination",
    "FastAPIPaginationCursor",
    "FastAPIPaginationLimitOffset",
    "LimitOffsetPage",
    "LimitOffsetParams",
    "Page",
    "PageLinks",
    "PageParams",
    "Position",
]
