"""Pagination by limit and offset, with the total: `?limit=20&offset=40`."""

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
    window,
)


@dataclass(frozen=True, slots=True)
class LimitOffsetParams(PaginationParams):
    """What `PaginationLimitOffset` reads from the query: `limit` and `offset`."""

    limit: int = 50
    offset: int = 0
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.limit < 1 or self.offset < 0:
            raise ValueError("limit starts at 1 and offset at 0")


class LimitOffsetPage(LinkedPage, Generic[T]):
    """A page of `items` by limit and offset, with the total."""

    items: list[T]
    total: int = Field(description="Items in every page together")
    limit: int = Field(description="Items in the page")
    offset: int = Field(description="Items before the page")

    def relations(self) -> list[Relation]:
        # first and last on the grid of `limit` from 0, where clients that start at first walk.
        last = (max(self.total, 1) - 1) // self.limit * self.limit
        offsets = [("first", 0)]
        if self.offset > 0:
            offsets.append(("prev", min(max(self.offset - self.limit, 0), last)))
        if self.offset + self.limit < self.total:
            offsets.append(("next", self.offset + self.limit))
        offsets.append(("last", last))
        return [(rel, {"limit": self.limit, "offset": offset}) for rel, offset in offsets]


class PaginationLimitOffset:
    """Pagination by limit and offset, with the total: `?limit=20&offset=40`. Without the total,
    which takes a count of the whole list: `PaginationOffset`.

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
                query(
                    "limit",
                    int,
                    default_limit,
                    ge=1,
                    le=max_limit,
                    description="Items in the page",
                ),
                query("offset", int, 0, ge=0, description="Items before the page"),
            ],
            return_annotation=LimitOffsetParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        limit: int | None = None,
        offset: int = 0,
    ) -> LimitOffsetParams:
        params = self.params(limit=limit, offset=offset)
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> LimitOffsetParams:
        """The parameters of a page, as the dependency gives them (for tests and scripts)."""
        limit = self.default_limit if limit is None else limit
        if limit > self.max_limit:
            raise ValueError(f"limit can't be over {self.max_limit}")
        return LimitOffsetParams(limit=limit, offset=offset)

    def paginate(
        self,
        items: Sequence[T],
        params: LimitOffsetParams,
        *,
        total: int | None = None,
    ) -> LimitOffsetPage[T]:
        """The page of `items` that `params` asks for, as `PaginationPageNumber.paginate`. The
        first and last pages that the response links to start at multiples of the limit."""
        page, total = window(items, params.offset, params.limit, total)
        return linked(
            LimitOffsetPage(items=page, total=total, limit=params.limit, offset=params.offset),
            params.links,
        )
