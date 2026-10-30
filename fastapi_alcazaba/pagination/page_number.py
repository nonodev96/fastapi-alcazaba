"""Pagination by page number: `?page=2&page_size=20`, with the total and the number of pages."""

import inspect
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from math import ceil
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
class PageNumberParams(PaginationParams):
    """What `PaginationPageNumber` reads from the query: `page` (from 1) and `page_size`."""

    page: int = 1
    page_size: int = 50
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.page < 1 or self.page_size < 1:
            raise ValueError("page and page_size start at 1")

    @property
    def offset(self) -> int:
        """Items before the page, for a database query."""
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        """Items in the page, for a database query."""
        return self.page_size


class PageNumberPage(LinkedPage, Generic[T]):
    """A page of `items` by page number: `PageNumberPage[Product]` as the endpoint's return type."""

    items: list[T]
    total: int = Field(description="Items in every page together")
    page: int = Field(description="Page number, from 1")
    page_size: int = Field(description="Items per page")
    pages: int = Field(description="Number of pages")

    def relations(self) -> list[Relation]:
        last = max(self.pages, 1)
        numbers = [("first", 1)]
        if self.page > 1:
            numbers.append(("prev", min(self.page - 1, last)))
        if self.page < self.pages:
            numbers.append(("next", self.page + 1))
        numbers.append(("last", last))
        return [(rel, {"page": number, "page_size": self.page_size}) for rel, number in numbers]


class PaginationPageNumber:
    """Pagination by page number: `?page=2&page_size=20`.

        pages = PaginationPageNumber()

        @app.get("/products")
        @cache(expire=60)
        async def products(
            params: Annotated[PageNumberParams, Depends(pages)],
        ) -> PageNumberPage[Product]:
            return pages.paginate(rows, params, total=count)

    :param default_page_size: items per page when the request doesn't say.
    :param max_page_size: the most a request may ask for (a 422 beyond), so that nobody asks for
        a million rows at once.
    """

    def __init__(
        self,
        *,
        default_page_size: int = 50,
        max_page_size: int = 100,
    ) -> None:
        check_limits(default_page_size, max_page_size, "page_size")
        self.default_page_size = default_page_size
        self.max_page_size = max_page_size
        # What FastAPI reads to know the query parameters: with this instance's limits.
        self.__signature__ = inspect.Signature(
            [
                *REQUEST,
                query("page", int, 1, ge=1, description="Page number, from 1"),
                query(
                    "page_size",
                    int,
                    default_page_size,
                    ge=1,
                    le=max_page_size,
                    description="Items per page",
                ),
            ],
            return_annotation=PageNumberParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        page: int = 1,
        page_size: int | None = None,
    ) -> PageNumberParams:
        params = self.params(page=page, page_size=page_size)
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        page: int = 1,
        page_size: int | None = None,
    ) -> PageNumberParams:
        """The parameters of a page, as the dependency gives them (for tests and scripts)."""
        page_size = self.default_page_size if page_size is None else page_size
        if page_size > self.max_page_size:
            raise ValueError(f"page_size can't be over {self.max_page_size}")
        return PageNumberParams(page=page, page_size=page_size)

    def paginate(
        self,
        items: Sequence[T],
        params: PageNumberParams,
        *,
        total: int | None = None,
    ) -> PageNumberPage[T]:
        """The page of `items` that `params` asks for. Without `total`, `items` is the whole
        list and the page is sliced from it; with `total`, `items` is that page already (queried
        with `params.offset` and `params.limit`). The response links to the first, previous,
        next and last pages."""
        page, total = window(items, params.offset, params.limit, total)
        return linked(
            PageNumberPage(
                items=page,
                total=total,
                page=params.page,
                page_size=params.page_size,
                pages=ceil(total / params.page_size),
            ),
            params.links,
        )
