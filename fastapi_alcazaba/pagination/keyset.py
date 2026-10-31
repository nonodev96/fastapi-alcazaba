"""Pagination by keyset, with the keys in plain sight: `?after=42&limit=20`, the items whose sort
key comes after the last one seen. The key is an id, a time (`?after=2026-10-29T10:00:00Z`), or
several values as a JSON array (`?after=["2026-10-29T10:00:00Z",42]`), which clients read and
write themselves, neither encoded nor signed, converted to the types that the endpoint declares.
A database finds the page through an index, and rows added or deleted meanwhile don't shift the
pages. Opaque, signed and in both directions: `pagination.cursor`."""

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
    Position,
)

#: A sort value as JSON: datetimes, dates, UUIDs and decimals as strings.
KeyValue = str | int | float | bool


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
) -> KeyValue:
    """`value`, a `kind`, as JSON, the way the items show it ("2026-10-29T10:00:00Z"). Raises
    TypeError if it isn't one."""
    dumped = to_jsonable_python(value) if isinstance(value, date | UUID | Decimal) else value
    if not isinstance(dumped, KeyValue) or not _loads(kind, dumped):
        raise TypeError(f"the sort key has {value!r} where sort says {kind.__name__}")
    return dumped


@dataclass(frozen=True, slots=True)
class KeysetParams(PaginationParams):
    """What `PaginationKeyset` reads from the query: `limit`, and `after`, the sort key of the
    last item seen, converted to its types (none for the first page): a value, or a tuple of
    them when the endpoint sorts by several."""

    limit: int = 50
    after: Position | None = None
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit starts at 1")
        if self.after == ():
            raise ValueError("after holds at least one value")

    @property
    def fetch(self) -> int:
        """Rows to query from the database: `limit` and one more, which tells whether there's a
        next page."""
        return self.limit + 1


class KeysetPage(LinkedPage, Generic[T]):
    """A page of `items` by keyset: `after` is the sort key of its last item, where the next page
    starts (None when it's empty), and `has_more` is False on the last one. A client that syncs
    keeps `after` from the last page, and asks with it later for what came since."""

    items: list[T]
    limit: int = Field(description="Items per page")
    after: str | None = Field(
        description="The sort key of the last item, as ?after= takes it; None when it's empty",
    )
    has_more: bool = Field(description="Whether there's a page after this one")

    def relations(self) -> list[Relation]:
        keys: list[tuple[str, str | None]] = [("first", None)]
        if self.has_more:
            keys.append(("next", self.after))
        return [(rel, {"after": after, "limit": self.limit}) for rel, after in keys]


class PaginationKeyset:
    """Pagination by keyset, with the keys in plain sight: `?limit=20`, then
    `?after=<after>&limit=20`.

        messages = PaginationKeyset(sort=int)  # by id: ?after=42

        @app.get("/messages")
        @cache(expire=60)
        async def list_messages(
            params: Annotated[KeysetParams, Depends(messages)],
        ) -> KeysetPage[Message]:
            rows = ...  # WHERE id > params.after ORDER BY id LIMIT params.fetch
            return messages.paginate(rows, params, key=lambda m: m.id, queried=True)

        events = PaginationKeyset(sort=(datetime, int))  # ?after=["2026-10-29T10:00:00Z",42]

    The page gives the last item's key as `after`, and a client can as well start after a key of
    its own (the events after a date). Keys are neither encoded nor signed, so sort by what
    clients may see. Each value is converted to the type `sort` declares, and a value of another
    type, another number of them or JSON that doesn't parse is a 400, before the endpoint runs.

    :param sort: the type of the sort key (`int`, `str`, `UUID`, `datetime`...), which `after`
        holds as text (`42`, `2026-10-29T10:00:00Z`); or the types of its values, in order, for a
        key of several (`(datetime, int)`), which `after` holds as a JSON array. Types: `int`,
        `float`, `str`, `bool`, `datetime` (with a time zone), `date`, `UUID` or `Decimal`; in
        JSON, datetimes and dates in ISO 8601, UUIDs and decimals as strings (which keep every
        digit of a decimal), the rest as themselves.
    :param default_limit: items per page when the request doesn't say.
    :param max_limit: the most a request may ask for (a 422 beyond).
    """

    def __init__(
        self,
        sort: type[CursorValue] | Sequence[type[CursorValue]],
        *,
        default_limit: int = 50,
        max_limit: int = 100,
    ) -> None:
        check_limits(default_limit, max_limit, "limit")
        self.single = isinstance(sort, type)
        kinds: tuple[type, ...] = (sort,) if isinstance(sort, type) else tuple(sort)
        if not 1 <= len(kinds) <= MAX_VALUES:
            raise ValueError(f"sort has 1 to {MAX_VALUES} types")
        for kind in kinds:
            if kind not in _LOADERS:
                raise TypeError(f"a sort value can't be a {getattr(kind, '__name__', kind)}")
        self.sort = kinds
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.__signature__ = inspect.Signature(
            [
                *REQUEST,
                query(
                    "after",
                    str | None,
                    None,
                    max_length=MAX_TOKEN,
                    description=(
                        "Where the page starts: the sort key of the last item seen (the after"
                        " of the page before)"
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
            return_annotation=KeysetParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        after: str | None = None,
        limit: int | None = None,
    ) -> KeysetParams:
        params = self.params(after=after, limit=limit)
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> KeysetParams:
        """The parameters of a page, with `after` converted, as the dependency gives them.
        Raises an HTTPException (400) if it isn't valid."""
        limit = self.default_limit if limit is None else limit
        if limit > self.max_limit:
            raise ValueError(f"limit can't be over {self.max_limit}")
        return KeysetParams(limit=limit, after=None if after is None else self.decode(after))

    def decode(
        self,
        after: str,
    ) -> Position:
        """The sort key in `after`, converted to the types of `sort`: a value as text, or a JSON
        array of them. Raises an HTTPException (400) if it isn't that."""
        try:
            if len(after) > MAX_TOKEN:
                raise ValueError("too long")
            if self.single:
                return _parse(self.sort[0], after)
            values = json.loads(after, parse_float=Decimal, parse_constant=_no_constants)
            if not isinstance(values, list) or len(values) != len(self.sort):
                raise ValueError("not as many values as sort has")
            return tuple(
                _LOADERS[kind](value) for kind, value in zip(self.sort, values, strict=True)
            )
        except ValueError:  # json.JSONDecodeError is one
            raise HTTPException(400, "Invalid after") from None

    def encode(
        self,
        position: Position,
    ) -> str:
        """`position` as `after` holds it. Raises TypeError if it isn't of the types of `sort`."""
        if self.single:
            value = _dump(self.sort[0], position)
            return value if isinstance(value, str) else json.dumps(value)
        if not isinstance(position, tuple) or len(position) != len(self.sort):
            raise TypeError(f"the sort key must be a tuple of {len(self.sort)} values, as sort")
        values = [_dump(kind, value) for kind, value in zip(self.sort, position, strict=True)]
        return json.dumps(values, separators=(",", ":"))

    def paginate(
        self,
        items: Sequence[T],
        params: KeysetParams,
        *,
        key: Callable[[T], Position],
        descending: bool = False,
        queried: bool = False,
    ) -> KeysetPage[T]:
        """The page of `items` that `params` asks for.

        :param key: an item's sort key, of the types of `sort`: unique (`m.id`, or
            `(e.created_at, e.id)`, not `e.created_at` alone, which two items may share).
        :param descending: `items` go from the greatest key to the smallest (newest first).
        :param queried: `items` is what the database returned for this page: up to
            `params.fetch` rows with a key past `params.after`, in order. Otherwise `items` is
            the whole list, sorted by `key`, and the page is found in it.
        """
        if queried:
            rows = list(items)
            page, more = rows[: params.limit], len(rows) > params.limit
        else:
            after, start = params.after, 0
            if after is not None:
                try:
                    start = next(
                        (
                            index
                            for index, item in enumerate(items)
                            if (key(item) < after if descending else key(item) > after)  # type: ignore[operator]
                        ),
                        len(items),
                    )
                except TypeError:  # a key of another kind than the list's
                    raise HTTPException(400, "Invalid after") from None
            page = list(items[start : start + params.limit])
            more = start + params.limit < len(items)
        return linked(
            KeysetPage(
                items=page,
                limit=params.limit,
                after=self.encode(key(page[-1])) if page else None,
                has_more=more,
            ),
            params.links,
        )


def _parse(
    kind: type,
    text: str,
) -> CursorValue:
    """A value of `kind` written as text: strings, datetimes, dates and UUIDs as themselves,
    numbers and booleans as JSON writes them. Raises ValueError when it isn't one."""
    if kind in (str, datetime, date, UUID):
        return _LOADERS[kind](text)
    return _LOADERS[kind](json.loads(text, parse_float=Decimal, parse_constant=_no_constants))
