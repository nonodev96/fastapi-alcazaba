"""Pagination by range: `?start=0&end=24`, the indexes of the first and last items (from 0, both
included), answered with the total and a `Content-Range: items 0-24/319` header: for data grids
and tables that ask for the rows they show."""

import inspect
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Generic

from fastapi import HTTPException, Request, Response
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
class RangeParams(PaginationParams):
    """What `PaginationRange` reads from the query: `start` and `end`, the indexes of the first
    and last items, from 0 and both included."""

    start: int = 0
    end: int = 49
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("start starts at 0, and end can't be before it")

    @property
    def offset(self) -> int:
        """Items before the range, for a database query."""
        return self.start

    @property
    def limit(self) -> int:
        """Items in the range, for a database query."""
        return self.end - self.start + 1


class RangePage(LinkedPage, Generic[T]):
    """A range of `items`, with the total, which the response repeats in `Content-Range`."""

    items: list[T]
    start: int = Field(description="Index of the first item, from 0")
    end: int | None = Field(description="Index of the last item; None when there's none")
    total: int = Field(description="Items in every range together")
    limit: int = Field(description="Items asked for: the size of the ranges the links point at")

    def relations(self) -> list[Relation]:
        # first and last on the grid of `limit` from 0, where clients that start at first walk.
        last = (max(self.total, 1) - 1) // self.limit * self.limit
        starts = [("first", 0)]
        if self.start > 0:
            starts.append(("prev", min(max(self.start - self.limit, 0), last)))
        if self.start + self.limit < self.total:
            starts.append(("next", self.start + self.limit))
        starts.append(("last", last))
        return [(rel, {"start": start, "end": start + self.limit - 1}) for rel, start in starts]

    def headers(self) -> dict[str, str]:
        if self.end is None:
            return {"Content-Range": f"items */{self.total}"}
        return {"Content-Range": f"items {self.start}-{self.end}/{self.total}"}


class PaginationRange:
    """Pagination by range: `?start=0&end=24`, both included.

        ranges = PaginationRange()

        @app.get("/contacts")
        @cache(expire=60)
        async def contacts(params: Annotated[RangeParams, Depends(ranges)]) -> RangePage[Contact]:
            count = ...  # SELECT count(*)
            rows = ...  # ORDER BY name LIMIT params.limit OFFSET params.offset
            return ranges.paginate(rows, params, total=count)

    The response says which items it holds in `Content-Range: items 0-24/319` (`items */319`
    when it holds none), on top of the page's `start`, `end` and `total`. A range that ends
    before it starts, or holds more than `max_items` items, is a 400; one that starts past the
    last item a 416, with `Content-Range: items */<total>`.

    :param default_items: items in the range when the request gives no `end`.
    :param max_items: the most a range may hold (a 400 beyond).
    """

    def __init__(
        self,
        *,
        default_items: int = 50,
        max_items: int = 100,
    ) -> None:
        check_limits(default_items, max_items, "items")
        self.default_items = default_items
        self.max_items = max_items
        self.__signature__ = inspect.Signature(
            [
                *REQUEST,
                query("start", int, 0, ge=0, description="Index of the first item, from 0"),
                query(
                    "end",
                    int | None,
                    None,
                    ge=0,
                    description=(
                        f"Index of the last item, included: start + {default_items - 1} when"
                        f" left out, and at most {max_items} items in all"
                    ),
                ),
            ],
            return_annotation=RangeParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        start: int = 0,
        end: int | None = None,
    ) -> RangeParams:
        params = self.params(start=start, end=end)
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        start: int = 0,
        end: int | None = None,
    ) -> RangeParams:
        """The parameters of a range, as the dependency gives them (for tests and scripts).
        Raises an HTTPException (400) if `end` is before `start`, or the range holds more than
        `max_items` items."""
        end = start + self.default_items - 1 if end is None else end
        if end < start:
            raise HTTPException(400, "Invalid range: end is before start")
        if end - start + 1 > self.max_items:
            raise HTTPException(400, f"Invalid range: more than {self.max_items} items")
        return RangeParams(start=start, end=end)

    def paginate(
        self,
        items: Sequence[T],
        params: RangeParams,
        *,
        total: int | None = None,
    ) -> RangePage[T]:
        """The range of `items` that `params` asks for. Without `total`, `items` is the whole
        list and the range is sliced from it; with `total`, `items` is that range already
        (queried with `params.offset` and `params.limit`). The response links to the first,
        previous, next and last ranges of the same size. Raises an HTTPException (416) when the
        range starts past the last item of a list that isn't empty."""
        page, total = window(items, params.offset, params.limit, total)
        if params.start >= total > 0:
            raise HTTPException(
                416,
                "Range Not Satisfiable",
                headers={"Content-Range": f"items */{total}"},
            )
        return linked(
            RangePage(
                items=page,
                start=params.start,
                end=params.start + len(page) - 1 if page else None,
                total=total,
                limit=params.limit,
            ),
            params.links,
        )
