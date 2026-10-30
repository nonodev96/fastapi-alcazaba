"""Pagination by offset, without the total: `?offset=40&limit=20`. One row more than the page tells
whether there's a next one, so the list is never counted: for lists whose count is slow, or that
change while they're read."""

import inspect
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Generic

from fastapi import Request, Response
from pydantic import Field

from fastapi_alcazaba.pagination.common import (
    REQUEST,
    LinkedPage,
    PageLinks,
    PaginationParams,
    Relation,
    T,
    check_limits,
    linked,
    query,
)


@dataclass(frozen=True, slots=True)
class OffsetParams(PaginationParams):
    """What `PaginationOffset` reads from the query: `offset` and `limit`."""

    offset: int = 0
    limit: int = 50
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.offset < 0 or self.limit < 1:
            raise ValueError("offset starts at 0 and limit at 1")

    @property
    def fetch(self) -> int:
        """Rows to query from the database: `limit` and one more, which tells whether there's a
        next page."""
        return self.limit + 1


class OffsetPage(LinkedPage, Generic[T]):
    """A page of `items` by offset, without the total; `has_more` is False on the last one."""

    items: list[T]
    offset: int = Field(description="Items before the page")
    limit: int = Field(description="Items per page")
    has_more: bool = Field(description="Whether there's a page after this one")

    def relations(self) -> list[Relation]:
        offsets = [("first", 0)]
        if self.offset > 0:
            offsets.append(("prev", max(self.offset - self.limit, 0)))
        if self.has_more:
            offsets.append(("next", self.offset + self.limit))
        return [(rel, {"offset": offset, "limit": self.limit}) for rel, offset in offsets]


class PaginationOffset:
    """Pagination by offset, without the total: `?offset=40&limit=20`.

        offsets = PaginationOffset()

        @app.get("/logs")
        @cache(expire=60)
        async def logs(params: Annotated[OffsetParams, Depends(offsets)]) -> OffsetPage[Line]:
            rows = ...  # ORDER BY id LIMIT params.fetch OFFSET params.offset
            return offsets.paginate(rows, params, queried=True)

    The page says whether there's a next one (`has_more`), from one row more than it holds,
    instead of counting the whole list: no `COUNT(*)`, which is slow on big tables and outdated
    as soon as rows are written. With the total and the last page: `PaginationLimitOffset`.

    :param default_limit: items per page when the request doesn't say.
    :param max_limit: the most a request may ask for (a 422 beyond).
    """

    def __init__(
        self,
        *,
        default_limit: int = 50,
        max_limit: int = 100,
    ) -> None:
        check_limits(default_limit, max_limit, "limit")
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.__signature__ = inspect.Signature(
            [
                *REQUEST,
                query("offset", int, 0, ge=0, description="Items before the page"),
                query(
                    "limit",
                    int,
                    default_limit,
                    ge=1,
                    le=max_limit,
                    description="Items per page",
                ),
            ],
            return_annotation=OffsetParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        offset: int = 0,
        limit: int | None = None,
    ) -> OffsetParams:
        params = self.params(offset=offset, limit=limit)
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> OffsetParams:
        """The parameters of a page, as the dependency gives them (for tests and scripts)."""
        limit = self.default_limit if limit is None else limit
        if limit > self.max_limit:
            raise ValueError(f"limit can't be over {self.max_limit}")
        return OffsetParams(offset=offset, limit=limit)

    def paginate(
        self,
        items: Sequence[T],
        params: OffsetParams,
        *,
        queried: bool = False,
    ) -> OffsetPage[T]:
        """The page of `items` that `params` asks for, linked to the first, previous and next
        pages.

        :param queried: `items` is what the database returned for this page: the rows of
            `LIMIT params.fetch OFFSET params.offset`, one more than the page when there's a
            next one. Otherwise `items` is the whole list, and the page is sliced from it.
        """
        if queried:
            rows = list(items)
        else:
            rows = list(items[params.offset : params.offset + params.fetch])
        return linked(
            OffsetPage(
                items=rows[: params.limit],
                offset=params.offset,
                limit=params.limit,
                has_more=len(rows) > params.limit,
            ),
            params.links,
        )
