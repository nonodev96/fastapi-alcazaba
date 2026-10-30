"""Pagination by id: `?last_id=42&limit=20`, the items whose id comes after the last one seen. Rows
added or deleted meanwhile don't shift the pages, and a database finds any page through the
primary key, as fast as the first."""

import inspect
import operator
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Generic
from uuid import UUID

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

#: An item's id: what `last_id` holds.
Id = int | str | UUID

# What the query parameter accepts besides its type: integers as databases sort them (larger
# ones from a client would overflow), and strings of a reasonable length.
_CONSTRAINTS: dict[type[Id], dict[str, Any]] = {
    int: {"ge": -(2**63), "le": 2**63 - 1},
    str: {"min_length": 1, "max_length": 255},
    UUID: {},
}


@dataclass(frozen=True, slots=True)
class IdBasedParams(PaginationParams):
    """What `PaginationIdBased` reads from the query: `last_id`, the id of the last item seen
    (none for the first page), and `limit`."""

    last_id: Id | None = None
    limit: int = 50
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit starts at 1")

    @property
    def fetch(self) -> int:
        """Rows to query from the database: `limit` and one more, which tells whether there's a
        next page."""
        return self.limit + 1


class IdBasedPage(LinkedPage, Generic[T]):
    """A page of `items` by id: `last_id` is where the next one starts, and `has_more` is False
    on the last one."""

    items: list[T]
    limit: int = Field(description="Items per page")
    last_id: Id | None = Field(description="Id of the page's last item; None when it's empty")
    has_more: bool = Field(description="Whether there's a page after this one")

    def relations(self) -> list[Relation]:
        ids: list[tuple[str, Id | None]] = [("first", None)]
        if self.has_more:
            ids.append(("next", self.last_id))
        return [(rel, {"last_id": last_id, "limit": self.limit}) for rel, last_id in ids]


class PaginationIdBased:
    """Pagination by id: `?limit=20`, then `?last_id=<last_id>&limit=20`.

        ids = PaginationIdBased()

        @app.get("/messages")
        @cache(expire=60)
        async def messages(
            params: Annotated[IdBasedParams, Depends(ids)],
        ) -> IdBasedPage[Message]:
            rows = ...  # WHERE id > params.last_id ORDER BY id LIMIT params.fetch
            return ids.paginate(rows, params, key=lambda m: m.id, queried=True)

    The id travels as it is, neither encoded nor signed: ids are public, and clients that keep
    the last one they've seen can ask for whatever came after it. To sort by other columns, or
    keep the sort key from clients: `PaginationCursor`.

    :param id_type: the type of the ids, and of the query parameter: `int`, `str` or `UUID`. A
        `last_id` of another type is a 422.
    :param default_limit: items per page when the request doesn't say.
    :param max_limit: the most a request may ask for (a 422 beyond).
    """

    def __init__(
        self,
        id_type: type[Id] = int,
        *,
        default_limit: int = 50,
        max_limit: int = 100,
    ) -> None:
        if id_type not in _CONSTRAINTS:
            raise ValueError("id_type is int, str or UUID")
        check_limits(default_limit, max_limit, "limit")
        self.id_type = id_type
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.__signature__ = inspect.Signature(
            [
                *REQUEST,
                query(
                    "last_id",
                    id_type | None,
                    None,
                    description="Id of the last item seen: the last_id of the page before",
                    **_CONSTRAINTS[id_type],
                ),
                query(
                    "limit",
                    int,
                    default_limit,
                    ge=1,
                    le=max_limit,
                    description="Items per page",
                ),
            ],
            return_annotation=IdBasedParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        last_id: Id | None = None,
        limit: int | None = None,
    ) -> IdBasedParams:
        params = self.params(last_id=last_id, limit=limit)
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        last_id: Id | None = None,
        limit: int | None = None,
    ) -> IdBasedParams:
        """The parameters of a page, as the dependency gives them (for tests and scripts)."""
        limit = self.default_limit if limit is None else limit
        if limit > self.max_limit:
            raise ValueError(f"limit can't be over {self.max_limit}")
        if last_id is not None and type(last_id) is not self.id_type:
            raise ValueError(f"last_id is a {self.id_type.__name__}")
        return IdBasedParams(last_id=last_id, limit=limit)

    def paginate(
        self,
        items: Sequence[T],
        params: IdBasedParams,
        *,
        key: Callable[[T], Id],
        descending: bool = False,
        queried: bool = False,
    ) -> IdBasedPage[T]:
        """The page of `items` that `params` asks for, linked to the first and next pages.

        :param key: an item's id, unique.
        :param descending: `items` go from the greatest id to the smallest (newest first), and
            each page holds the items whose id is smaller than `last_id`.
        :param queried: `items` is what the database returned for this page: up to
            `params.fetch` rows with an id past `params.last_id`, in order
            (`WHERE id > :last_id ORDER BY id LIMIT :fetch`). Otherwise `items` is the whole
            list, sorted by id, and the page is found in it.
        """
        if queried:
            rows = list(items)
            page, more = rows[: params.limit], len(rows) > params.limit
        else:
            start = 0
            if params.last_id is not None:
                past = operator.lt if descending else operator.gt
                start = next(
                    (index for index, item in enumerate(items) if past(key(item), params.last_id)),
                    len(items),
                )
            page = list(items[start : start + params.limit])
            more = start + params.limit < len(items)
        return linked(
            IdBasedPage(
                items=page,
                limit=params.limit,
                last_id=key(page[-1]) if page else None,
                has_more=more,
            ),
            params.links,
        )
