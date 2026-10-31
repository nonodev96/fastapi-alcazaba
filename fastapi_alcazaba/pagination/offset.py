"""Pagination by offset: `?offset=40&limit=20`, with the total when the endpoint counts the list,
or without it, when one row more than the page tells whether there's a next one: for lists whose
count is slow, or that change while they're read."""

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
        """Rows to query from the database without counting the list: `limit` and one more,
        which tells whether there's a next page."""
        return self.limit + 1


class OffsetPage(LinkedPage, Generic[T]):
    """A page of `items` by offset; `has_more` is False on the last one, and `total` is None when
    the list wasn't counted."""

    items: list[T]
    offset: int = Field(description="Items before the page")
    limit: int = Field(description="Items per page")
    has_more: bool = Field(description="Whether there's a page after this one")
    total: int | None = Field(description="Items in every page together; None if not counted")

    def relations(self) -> list[Relation]:
        offsets = [("first", 0)]
        # prev and last on the grid of `limit` from 0, where clients that start at first walk.
        last = None if self.total is None else (max(self.total, 1) - 1) // self.limit * self.limit
        if self.offset > 0:
            before = max(self.offset - self.limit, 0)
            offsets.append(("prev", before if last is None else min(before, last)))
        if self.has_more:
            offsets.append(("next", self.offset + self.limit))
        if last is not None:
            offsets.append(("last", last))
        return [(rel, {"offset": offset, "limit": self.limit}) for rel, offset in offsets]


class PaginationOffset:
    """Pagination by offset: `?offset=40&limit=20`.

        offsets = PaginationOffset()

        @app.get("/products")
        @cache(expire=60)
        async def products(params: Annotated[OffsetParams, Depends(offsets)]) -> OffsetPage[Item]:
            rows = ...  # ORDER BY id LIMIT params.limit OFFSET params.offset
            return offsets.paginate(rows, params, total=...)  # SELECT count(*)

    With `total`, the page has it and links to the last page too. Without it, query
    `params.fetch` rows (one more than the page) and pass `queried=True`: the extra row says
    whether there's a next page, without a `COUNT(*)`, slow on big tables and outdated as soon as
    rows are written.

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
        total: int | None = None,
        queried: bool = False,
    ) -> OffsetPage[T]:
        """The page of `items` that `params` asks for, linked to the first, previous and next
        pages, and to the last one when the total is known.

        :param total: the count of the whole list: `items` is then the page, as the database
            returned it (`LIMIT params.limit OFFSET params.offset`).
        :param queried: without a total, `items` is what the database returned for this page:
            the rows of `LIMIT params.fetch OFFSET params.offset`, one more than the page when
            there's a next one.

        With neither, `items` is the whole list: the page is sliced from it, and counted.
        """
        if total is not None:
            if total < 0:
                raise ValueError("total can't be negative")
            rows = list(items)
            more = params.offset + params.limit < total
        elif queried:
            rows = list(items)
            more = len(rows) > params.limit
        else:
            total = len(items)
            rows = list(items[params.offset : params.offset + params.limit])
            more = params.offset + params.limit < total
        return linked(
            OffsetPage(
                items=rows[: params.limit],
                offset=params.offset,
                limit=params.limit,
                has_more=more,
                total=total,
            ),
            params.links,
        )
