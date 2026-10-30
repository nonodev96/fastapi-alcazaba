"""Pagination after the sort values of the last item seen:
`?search_after=["2026-10-29T10:00:00Z",42]&limit=20`. The values are a JSON array that
clients read and write themselves, neither encoded nor signed, converted to the types that the
endpoint declares: a database finds the page through an index, and rows added or deleted
meanwhile don't shift the pages."""

import inspect
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Generic
from uuid import UUID

from fastapi import HTTPException, Request, Response
from pydantic import Field
from pydantic_core import to_jsonable_python

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
from fastapi_alcazaba.pagination.values import (
    MAX_INT,
    MAX_TOKEN,
    MAX_VALUES,
    CursorValue,
)

#: A sort value as JSON: datetimes, dates, UUIDs and decimals as strings.
SearchValue = str | int | float | bool


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("not true or false")
    return value


def _int(value: object) -> int:
    if type(value) is not int or not -MAX_INT <= value < MAX_INT:
        raise ValueError("not an integer of up to 64 bits")
    return value


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise ValueError("not a number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError("a number too large") from None
    if not isfinite(number):
        raise ValueError("a number too large")
    return number


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("not a string")
    return value


def _datetime(value: object) -> datetime:
    moment = datetime.fromisoformat(_str(value))
    if moment.utcoffset() is None:
        raise ValueError("a datetime without a time zone")
    return moment


def _date(value: object) -> date:
    return date.fromisoformat(_str(value))


def _uuid(value: object) -> UUID:
    return UUID(_str(value))


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise ValueError("not a number")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{value!r} isn't a number") from None
    if not number.is_finite():
        raise ValueError("NaN or infinity")
    return number


# How a value from the JSON array becomes each type: a ValueError when it can't. JSON's numbers
# come as Decimal, exact, until they're converted.
_LOADERS: dict[type, Callable[[object], CursorValue]] = {
    bool: _bool,
    int: _int,
    float: _float,
    str: _str,
    datetime: _datetime,
    date: _date,
    UUID: _uuid,
    Decimal: _decimal,
}


def _no_constants(name: str) -> object:
    raise ValueError(f"{name} isn't a sort value")


def _loads(
    kind: type,
    value: object,
) -> bool:
    """Whether `value` is a `kind` as JSON."""
    try:
        _LOADERS[kind](value)
    except ValueError:
        return False
    return True


def _dump(
    kind: type,
    value: object,
) -> SearchValue:
    """`value`, a `kind`, as JSON, the way the items show it ("2026-10-29T10:00:00Z"). Raises
    TypeError if it isn't one."""
    dumped = to_jsonable_python(value) if isinstance(value, date | UUID | Decimal) else value
    if not isinstance(dumped, SearchValue) or not _loads(kind, dumped):
        raise TypeError(f"the sort key has {value!r} where sort says {kind.__name__}")
    return dumped


@dataclass(frozen=True, slots=True)
class SearchAfterParams(PaginationParams):
    """What `PaginationSearchAfter` reads from the query: `limit`, and `search_after`, the sort
    values of the last item seen, converted to their types (none for the first page)."""

    limit: int = 50
    search_after: tuple[CursorValue, ...] | None = None
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit starts at 1")
        if self.search_after == ():
            raise ValueError("search_after holds at least one value")

    @property
    def fetch(self) -> int:
        """Rows to query from the database: `limit` and one more, which tells whether there's a
        next page."""
        return self.limit + 1


class SearchAfterPage(LinkedPage, Generic[T]):
    """A page of `items` after sort values; `search_after` is None on the last one."""

    items: list[T]
    limit: int = Field(description="Items per page")
    search_after: list[SearchValue] | None = Field(
        description="The sort values of the last item, where the next page starts; None at the end",
    )

    def relations(self) -> list[Relation]:
        values: list[tuple[str, str | None]] = [("first", None)]
        if self.search_after is not None:
            values.append(("next", json.dumps(self.search_after, separators=(",", ":"))))
        return [(rel, {"search_after": text, "limit": self.limit}) for rel, text in values]


class PaginationSearchAfter:
    """Pagination after the sort values of the last item seen:
    `?search_after=["2026-10-29T10:00:00Z",42]&limit=20`.

        events = PaginationSearchAfter(sort=(datetime, int))

        @app.get("/events")
        @cache(expire=60)
        async def list_events(
            params: Annotated[SearchAfterParams, Depends(events)],
        ) -> SearchAfterPage[Event]:
            rows = ...  # WHERE (created_at, id) > params.search_after ... LIMIT params.fetch
            return events.paginate(rows, params, key=lambda e: (e.created_at, e.id), queried=True)

    The values are plain JSON that clients read and may write: the page gives the last item's
    as `search_after`, and a client can as well start after values of its own (the events after
    a date). They're neither encoded nor signed, so sort by what clients may see. Each is
    converted to the type `sort` declares, and a value of another type, another number of them
    or JSON that doesn't parse is a 400, before the endpoint runs.

    :param sort: the types of the sort values, in order: `int`, `float`, `str`, `bool`,
        `datetime` (with a time zone), `date`, `UUID` or `Decimal`. In JSON they're as the
        items show them: datetimes and dates in ISO 8601, UUIDs and decimals as strings (which
        keep every digit of a decimal), the rest as themselves.
    :param default_limit: items per page when the request doesn't say.
    :param max_limit: the most a request may ask for (a 422 beyond).
    """

    def __init__(
        self,
        sort: Sequence[type[CursorValue]],
        *,
        default_limit: int = 50,
        max_limit: int = 100,
    ) -> None:
        check_limits(default_limit, max_limit, "limit")
        if not 1 <= len(sort) <= MAX_VALUES:
            raise ValueError(f"sort has 1 to {MAX_VALUES} types")
        for kind in sort:
            if kind not in _LOADERS:
                raise TypeError(f"a sort value can't be a {getattr(kind, '__name__', kind)}")
        self.sort = tuple(sort)
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.__signature__ = inspect.Signature(
            [
                *REQUEST,
                query(
                    "search_after",
                    str | None,
                    None,
                    max_length=MAX_TOKEN,
                    description=(
                        "Where the page starts: the sort values of the last item seen, as a"
                        " JSON array (the search_after of the page before)"
                    ),
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
            return_annotation=SearchAfterParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        search_after: str | None = None,
        limit: int | None = None,
    ) -> SearchAfterParams:
        params = self.params(search_after=search_after, limit=limit)
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        search_after: str | None = None,
        limit: int | None = None,
    ) -> SearchAfterParams:
        """The parameters of a page, with `search_after` (JSON) converted, as the dependency
        gives them. Raises an HTTPException (400) if it isn't valid."""
        limit = self.default_limit if limit is None else limit
        if limit > self.max_limit:
            raise ValueError(f"limit can't be over {self.max_limit}")
        values = None if search_after is None else self.decode(search_after)
        return SearchAfterParams(limit=limit, search_after=values)

    def decode(
        self,
        search_after: str,
    ) -> tuple[CursorValue, ...]:
        """The sort values in `search_after`, a JSON array, converted to the types of `sort`.
        Raises an HTTPException (400) if they aren't those."""
        try:
            if len(search_after) > MAX_TOKEN:
                raise ValueError("too long")
            values = json.loads(search_after, parse_float=Decimal, parse_constant=_no_constants)
            if not isinstance(values, list) or len(values) != len(self.sort):
                raise ValueError("not as many values as sort has")
            return tuple(
                _LOADERS[kind](value) for kind, value in zip(self.sort, values, strict=True)
            )
        except ValueError:  # json.JSONDecodeError is one
            raise HTTPException(400, "Invalid search_after") from None

    def paginate(
        self,
        items: Sequence[T],
        params: SearchAfterParams,
        *,
        key: Callable[[T], tuple[CursorValue, ...]],
        descending: bool = False,
        queried: bool = False,
    ) -> SearchAfterPage[T]:
        """The page of `items` that `params` asks for.

        :param key: an item's sort values, of the types of `sort`: unique together
            (`(e.created_at, e.id)`, not `(e.created_at,)` alone, which two items may share).
        :param descending: `items` go from the greatest key to the smallest (newest first).
        :param queried: `items` is what the database returned for this page: up to
            `params.fetch` rows with a key past `params.search_after`, in order. Otherwise
            `items` is the whole list, sorted by `key`, and the page is found in it.
        """
        if queried:
            rows = list(items)
            page, more = rows[: params.limit], len(rows) > params.limit
        else:
            after, start = params.search_after, 0
            if after is not None:
                start = next(
                    (
                        index
                        for index, item in enumerate(items)
                        if (key(item) < after if descending else key(item) > after)
                    ),
                    len(items),
                )
            page = list(items[start : start + params.limit])
            more = start + params.limit < len(items)
        values = self._values(key(page[-1])) if more and page else None
        return linked(
            SearchAfterPage(items=page, limit=params.limit, search_after=values),
            params.links,
        )

    def _values(
        self,
        position: tuple[CursorValue, ...],
    ) -> list[SearchValue]:
        """The sort values of an item as JSON. Raises TypeError if they aren't those of `sort`."""
        if not isinstance(position, tuple) or len(position) != len(self.sort):
            raise TypeError(f"the sort key must be a tuple of {len(self.sort)} values, as sort")
        return [_dump(kind, value) for kind, value in zip(self.sort, position, strict=True)]
