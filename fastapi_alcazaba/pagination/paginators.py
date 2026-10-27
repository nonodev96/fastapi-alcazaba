"""Pagination with nothing but FastAPI and pydantic: by page number (`FastAPIPagination`), by
limit and offset (`FastAPIPaginationLimitOffset`) or by cursor (`FastAPIPaginationCursor`). Each
is configured once and used as a dependency:

    pages = FastAPIPagination(max_size=100)

    @app.get("/products")
    @cache(expire=60)
    async def products(params: Annotated[PageParams, Depends(pages)]) -> Page[Product]:
        return pages.paginate(await load_products(), params)

`GET /products?page=2&size=20` answers `{"items": [...], "total": 245, "page": 2, "size": 20,
"pages": 13}`, the JSON of fastapi-pagination's `Page` (and `LimitOffsetPage` that of its
namesake), so clients don't change. For a database, query only the page (`params.offset`,
`params.limit`) and give the total: `pages.paginate(rows, params, total=count)`.

Cursors point at the last item of a page by its sort key rather than by its position: rows added
or deleted meanwhile don't shift the pages, and a database finds any page through an index, as
fast as the first.

The page parameters are an argument of the endpoint, so the cache keeps each page under a key of
its own. fastapi-pagination's `add_pagination` keeps them out of the arguments, in a context
variable: a cache that only sees the arguments would serve page 1 for every page.

Responses link to the pages around them in the Link header (RFC 8288), as GitHub's API does:
`</products?page=3&size=20>; rel="next", </products?page=13&size=20>; rel="last"`.
"""

import base64
import binascii
import hashlib
import hmac
import inspect
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from math import ceil, isfinite
from typing import Annotated, Any, Generic, TypeVar
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from fastapi_alcazaba.core.integrity import Secrets, secret_list

T = TypeVar("T")

#: A value of an item's sort key that a cursor can hold.
CursorValue = str | int | float | bool | datetime | date | UUID | Decimal
#: Where a cursor points: the sort key of the last item of a page, one value or a tuple of them.
Position = CursorValue | tuple[CursorValue, ...]

_MAX_CURSOR = 1024  # characters of a cursor
_CURSOR_PATTERN = r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)?$"
_MAX_VALUES = 8  # values in a position
_MAX_INT = 2**63  # integers as databases sort them: larger ones from a client would overflow
# What a path keeps unquoted in a link: no "<", ">", "," or ";", which end it or the link.
_PATH_SAFE = "/:@!$&'()*+="


# region Links


@dataclass(frozen=True, slots=True)
class PageLinks:
    """Where the Link header of a page goes: the response to the request that asked for it, and
    the request's path and query parameters. The links keep the other parameters (filters,
    sorting) and are relative to the host (`</products?page=3&size=20>`), so they don't depend
    on the Host header nor on whether a proxy in front speaks HTTPS."""

    response: Response
    path: str
    query: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, request: Request, response: Response) -> "PageLinks":
        # The path as decoded and quoted again: nothing from the client ends the link early.
        path = quote(request.scope["path"], safe=_PATH_SAFE)
        return cls(response, path, tuple(request.query_params.multi_items()))

    def add(self, page: object) -> None:
        """Add the links of `page` to the response's Link header: first, prev, next and last of
        a Page or a LimitOffsetPage, first and next of a CursorPage. Anything else has none.
        """
        links = [self._link(rel, values) for rel, values in _relations(page)]
        if not links:
            return
        existing = self.response.headers.get("link")
        self.response.headers["Link"] = ", ".join([existing, *links] if existing else links)

    def _link(self, rel: str, values: dict[str, object]) -> str:
        """The link to the request's URL with `values` as page parameters (None leaves one out)."""
        query = [(name, value) for name, value in self.query if name not in values]
        query += [(name, str(value)) for name, value in values.items() if value is not None]
        return f'<{self.path}?{urlencode(query)}>; rel="{rel}"'


def _relations(page: object) -> list[tuple[str, dict[str, object]]]:
    """The pages around `page`, by relation, as their parameters."""
    if isinstance(page, Page):
        last = max(page.pages, 1)
        numbers = [("first", 1)]
        if page.page > 1:
            numbers.append(("prev", min(page.page - 1, last)))
        if page.page < page.pages:
            numbers.append(("next", page.page + 1))
        numbers.append(("last", last))
        return [(rel, {"page": number, "size": page.size}) for rel, number in numbers]
    if isinstance(page, LimitOffsetPage):
        # first and last on the grid of `limit` from 0, where clients that start at first walk.
        last = (max(page.total, 1) - 1) // page.limit * page.limit
        offsets = [("first", 0)]
        if page.offset > 0:
            offsets.append(("prev", min(max(page.offset - page.limit, 0), last)))
        if page.offset + page.limit < page.total:
            offsets.append(("next", page.offset + page.limit))
        offsets.append(("last", last))
        return [(rel, {"limit": page.limit, "offset": offset}) for rel, offset in offsets]
    if isinstance(page, CursorPage):
        cursors: list[tuple[str, str | None]] = [("first", None)]
        if page.next_cursor is not None:
            cursors.append(("next", page.next_cursor))
        return [(rel, {"cursor": cursor, "size": page.size}) for rel, cursor in cursors]
    return []


# endregion

# region Parameters and pages


@dataclass(frozen=True, slots=True)
class PageParams:
    """What `FastAPIPagination` reads from the query: `page` (from 1) and `size`."""

    page: int = 1
    size: int = 50
    #: Where `paginate` puts the Link header; None outside a request (`params()`).
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.page < 1 or self.size < 1:
            raise ValueError("page and size start at 1")

    @property
    def offset(self) -> int:
        """Items before the page, for a database query."""
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        """Items in the page, for a database query."""
        return self.size


@dataclass(frozen=True, slots=True)
class LimitOffsetParams:
    """What `FastAPIPaginationLimitOffset` reads from the query: `limit` and `offset`."""

    limit: int = 50
    offset: int = 0
    #: Where `paginate` puts the Link header; None outside a request (`params()`).
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.limit < 1 or self.offset < 0:
            raise ValueError("limit starts at 1 and offset at 0")


@dataclass(frozen=True, slots=True)
class CursorParams:
    """What `FastAPIPaginationCursor` reads from the query: `size` and the `cursor` (none for the
    first page), with `after`, the position it points at, already decoded."""

    size: int = 50
    cursor: str | None = None
    after: Position | None = None
    #: Where `paginate` puts the Link header; None outside a request (`params()`).
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError("size starts at 1")

    @property
    def limit(self) -> int:
        """Rows to query from the database: `size` and one more, which tells whether there's a
        next page."""
        return self.size + 1


class Page(BaseModel, Generic[T]):
    """A page of `items` by page number: `Page[Product]` as the endpoint's return type."""

    items: list[T]
    total: int = Field(description="Items in every page together")
    page: int = Field(description="Page number, from 1")
    size: int = Field(description="Items per page")
    pages: int = Field(description="Number of pages")


class LimitOffsetPage(BaseModel, Generic[T]):
    """A page of `items` by limit and offset."""

    items: list[T]
    total: int = Field(description="Items in every page together")
    limit: int = Field(description="Items in the page")
    offset: int = Field(description="Items before the page")


class CursorPage(BaseModel, Generic[T]):
    """A page of `items` by cursor; `next_cursor` is None on the last one."""

    items: list[T]
    size: int = Field(description="Items per page")
    next_cursor: str | None = Field(description="Where the next page starts; None at the end")


# endregion

# region Tools


def _limits(
    default: int,
    maximum: int,
    name: str,
) -> None:
    if not 1 <= default <= maximum:
        raise ValueError(f"the default {name} must be between 1 and the maximum")


def _query(
    name: str,
    kind: Any,
    default: Any,
    **constraints: Any,
) -> inspect.Parameter:
    return inspect.Parameter(
        name,
        inspect.Parameter.KEYWORD_ONLY,
        default=default,
        annotation=Annotated[kind, Query(**constraints)],
    )


def _window(
    items: Sequence[T],
    offset: int,
    limit: int,
    total: int | None,
) -> tuple[list[T], int]:
    """The page and the total: sliced from the whole list, or as queried with its total."""
    if total is None:
        return list(items[offset : offset + limit]), len(items)
    if total < 0:
        raise ValueError("total can't be negative")
    return list(items), total


# The request and FastAPI's response, for the links: no query parameters, nothing in OpenAPI.
_REQUEST = [
    inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY, annotation=Request),
    inspect.Parameter("response", inspect.Parameter.KEYWORD_ONLY, annotation=Response),
]

PageT = TypeVar("PageT", bound=BaseModel)


def _linked(page: PageT, links: PageLinks | None) -> PageT:
    if links is not None:
        links.add(page)
    return page


class FastAPIPagination:
    """Pagination by page number: `?page=2&size=20`.

        pages = FastAPIPagination()

        async def products(params: Annotated[PageParams, Depends(pages)]) -> Page[Product]:
            return pages.paginate(rows, params, total=count)

    :param default_size: items per page when the request doesn't say.
    :param max_size: the most a request may ask for (a 422 beyond), so that nobody asks for a
        million rows at once.
    """

    def __init__(
        self,
        *,
        default_size: int = 50,
        max_size: int = 100,
    ) -> None:
        _limits(default_size, max_size, "size")
        self.default_size = default_size
        self.max_size = max_size
        # What FastAPI reads to know the query parameters: with this instance's limits.
        self.__signature__ = inspect.Signature(
            [
                *_REQUEST,
                _query("page", int, 1, ge=1, description="Page number, from 1"),
                _query(
                    "size",
                    int,
                    default_size,
                    ge=1,
                    le=max_size,
                    description="Items per page",
                ),
            ],
            return_annotation=PageParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        page: int = 1,
        size: int | None = None,
    ) -> PageParams:
        return replace(self.params(page=page, size=size), links=PageLinks.of(request, response))

    def params(
        self,
        *,
        page: int = 1,
        size: int | None = None,
    ) -> PageParams:
        """The parameters of a page, as the dependency gives them (for tests and scripts)."""
        size = self.default_size if size is None else size
        if size > self.max_size:
            raise ValueError(f"size can't be over {self.max_size}")
        return PageParams(page=page, size=size)

    def paginate(
        self,
        items: Sequence[T],
        params: PageParams,
        *,
        total: int | None = None,
    ) -> Page[T]:
        """The page of `items` that `params` asks for. Without `total`, `items` is the whole
        list and the page is sliced from it; with `total`, `items` is that page already (queried
        with `params.offset` and `params.limit`). The response links to the first, previous,
        next and last pages."""
        page, total = _window(items, params.offset, params.limit, total)
        pages = ceil(total / params.size)
        return _linked(
            Page(items=page, total=total, page=params.page, size=params.size, pages=pages),
            params.links,
        )


class FastAPIPaginationLimitOffset:
    """Pagination by limit and offset: `?limit=20&offset=40`.

    :param default_limit: items per page when the request doesn't say.
    :param max_limit: the most a request may ask for (a 422 beyond).
    """

    def __init__(
        self,
        *,
        default_limit: int = 50,
        max_limit: int = 100,
    ) -> None:
        _limits(default_limit, max_limit, "limit")
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.__signature__ = inspect.Signature(
            [
                *_REQUEST,
                _query(
                    "limit",
                    int,
                    default_limit,
                    ge=1,
                    le=max_limit,
                    description="Items in the page",
                ),
                _query(
                    "offset",
                    int,
                    0,
                    ge=0,
                    description="Items before the page",
                ),
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
        """The page of `items` that `params` asks for, as `FastAPIPagination.paginate`. The
        first and last pages that the response links to start at multiples of the limit.
        """
        page, total = _window(items, params.offset, params.limit, total)
        return _linked(
            LimitOffsetPage(
                items=page,
                total=total,
                limit=params.limit,
                offset=params.offset,
            ),
            params.links,
        )


def _decimal(text: str) -> Decimal:
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{text!r} isn't a number") from None
    if not number.is_finite():
        raise ValueError("a cursor can't hold NaN or infinity")
    return number


# Types that JSON doesn't have travel tagged, and come back as themselves.
_TAGGED: dict[str, Callable[[str], CursorValue]] = {
    "datetime": datetime.fromisoformat,
    "date": date.fromisoformat,
    "uuid": UUID,
    "decimal": _decimal,
}


def _encode_value(value: object) -> object:
    if isinstance(value, int) and not isinstance(value, bool) and not -_MAX_INT <= value < _MAX_INT:
        raise ValueError("a cursor holds integers of up to 64 bits")
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float | Decimal) and not isfinite(value):
        raise ValueError("a cursor can't hold NaN or infinity")
    if isinstance(value, float):
        return value
    for tag, kind in (
        ("datetime", datetime),
        ("date", date),
        ("uuid", UUID),
        ("decimal", Decimal),
    ):
        if isinstance(value, kind):
            text = value.isoformat() if isinstance(value, date) else str(value)
            return {"t": tag, "v": text}
    raise TypeError(f"a cursor can't hold a {type(value).__name__}: give the sort key as values")


def _decode_value(value: object) -> CursorValue:
    if isinstance(value, int) and not isinstance(value, bool) and not -_MAX_INT <= value < _MAX_INT:
        raise ValueError("an integer of more than 64 bits")
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict) and value.keys() == {"t", "v"}:
        tag, text = value["t"], value["v"]
        if isinstance(tag, str) and isinstance(text, str) and tag in _TAGGED:
            return _TAGGED[tag](text)
    raise ValueError("not a value of a sort key")


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} isn't a value of a sort key")


class FastAPIPaginationCursor:
    """Pagination by cursor: `?size=20`, then `?size=20&cursor=<next_cursor>`.

        cursors = FastAPIPaginationCursor(secret=settings.cursor_secret)

        @app.get("/events")
        @cache(expire=60)
        async def events(params: Annotated[CursorParams, Depends(cursors)]) -> CursorPage[Event]:
            rows = ...  # WHERE (created_at, id) > params.after ... LIMIT params.limit
            return cursors.paginate(rows, params, key=lambda e: (e.created_at, e.id), queried=True)

    A cursor is the sort key of a page's last item, base64-encoded: clients can read it, so sort
    by what they may see. With a `secret` it's signed too (HMAC-SHA256), and a cursor that a
    client made or changed gets a 400 like any other invalid one, before the endpoint runs;
    without it, the values are only checked to be of the types a sort key can have.

    :param secret: at least 32 bytes (`secrets.token_urlsafe(32)`); a list rotates them, the
        first one signing and all of them accepted. None for unsigned cursors.
    :param default_size: items per page when the request doesn't say.
    :param max_size: the most a request may ask for (a 422 beyond).
    """

    def __init__(
        self,
        secret: Secrets | None = None,
        *,
        default_size: int = 50,
        max_size: int = 100,
    ) -> None:
        _limits(default_size, max_size, "size")
        self.default_size = default_size
        self.max_size = max_size
        # A key of their own: the secret may sign other things too.
        self._keys = (
            [
                hmac.new(key, b"fastapi_alcazaba.cursor", hashlib.sha256).digest()
                for key in secret_list(secret)
            ]
            if secret is not None
            else None
        )
        self.__signature__ = inspect.Signature(
            [
                *_REQUEST,
                _query(
                    "cursor",
                    str | None,
                    None,
                    max_length=_MAX_CURSOR,
                    pattern=_CURSOR_PATTERN,
                    description="Where the page starts: the next_cursor of the page before",
                ),
                _query(
                    "size",
                    int,
                    default_size,
                    ge=1,
                    le=max_size,
                    description="Items per page",
                ),
            ],
            return_annotation=CursorParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        cursor: str | None = None,
        size: int | None = None,
    ) -> CursorParams:
        params = self.params(cursor=cursor, size=size)
        return replace(params, links=PageLinks.of(request, response))

    def params(self, *, cursor: str | None = None, size: int | None = None) -> CursorParams:
        """The parameters of a page, with the cursor decoded, as the dependency gives them.
        Raises an HTTPException (400) if the cursor isn't valid."""
        size = self.default_size if size is None else size
        if size > self.max_size:
            raise ValueError(f"size can't be over {self.max_size}")
        after = None if cursor is None else self.decode(cursor)
        return CursorParams(size=size, cursor=cursor, after=after)

    def encode(
        self,
        position: Position,
    ) -> str:
        """The cursor of `position`."""
        if isinstance(position, tuple):
            if not 1 <= len(position) <= _MAX_VALUES:
                raise ValueError(f"a position has 1 to {_MAX_VALUES} values")
            encoded: object = [_encode_value(value) for value in position]
        else:
            encoded = _encode_value(position)
        text = json.dumps(encoded, separators=(",", ":"), allow_nan=False).encode()
        payload = base64.urlsafe_b64encode(text).rstrip(b"=").decode()
        if self._keys is None:
            return payload
        return f"{payload}.{self._tag(self._keys[0], payload)}"

    def decode(
        self,
        cursor: str,
    ) -> Position:
        """The position `cursor` points at. Raises an HTTPException (400) if it isn't valid."""
        # Cursors are ASCII (base64 and hex), and compare_digest refuses strings that aren't.
        if len(cursor) > _MAX_CURSOR or not cursor.isascii():
            raise HTTPException(400, "Invalid cursor")
        payload, _, tag = cursor.partition(".")
        if self._keys is not None and not any(
            hmac.compare_digest(tag, self._tag(key, payload)) for key in self._keys
        ):
            raise HTTPException(400, "Invalid cursor")
        try:
            text = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            decoded = json.loads(text, parse_constant=_no_constants)
            if isinstance(decoded, list):
                if not 1 <= len(decoded) <= _MAX_VALUES:
                    raise ValueError("too many values")
                return tuple(_decode_value(value) for value in decoded)
            return _decode_value(decoded)
        except (binascii.Error, ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid cursor") from None

    def paginate(
        self,
        items: Sequence[T],
        params: CursorParams,
        *,
        key: Callable[[T], Position],
        descending: bool = False,
        queried: bool = False,
    ) -> CursorPage[T]:
        """The page of `items` that `params` asks for.

        :param key: an item's sort key: a unique value or tuple (`(e.created_at, e.id)`, not
            `e.created_at` alone, which two items may share).
        :param descending: `items` go from the greatest key to the smallest (newest first).
        :param queried: `items` is what the database returned for this page: up to
            `params.limit` rows with a key past `params.after`, in order. Otherwise `items` is
            the whole list, sorted by `key`, and the page is found in it.
        """
        if queried:
            rows = list(items)
            page, more = rows[: params.size], len(rows) > params.size
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
                except TypeError:  # a cursor of another endpoint, with another kind of key
                    raise HTTPException(400, "Invalid cursor") from None
            page = list(items[start : start + params.size])
            more = start + params.size < len(items)
        next_cursor = self.encode(key(page[-1])) if more and page else None
        return _linked(
            CursorPage(
                items=page,
                size=params.size,
                next_cursor=next_cursor,
            ),
            params.links,
        )

    @staticmethod
    def _tag(
        key: bytes,
        payload: str,
    ) -> str:
        digest = hmac.new(key, payload.encode(), hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# endregion
