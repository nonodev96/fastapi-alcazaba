"""Pagination by cursor: `?limit=20`, then `?limit=20&cursor=<next_cursor>`. A cursor is the
sort key of a page's last item, opaque to clients and signed: rows added or deleted meanwhile
don't shift the pages, and a database finds any page through an index, as fast as the first."""

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Generic

from fastapi import HTTPException, Request, Response
from pydantic import Field

from fastapi_alcazaba.core.integrity import Secrets
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
    MAX_TOKEN,
    SEALED_PATTERN,
    Position,
    Sealer,
    decode_position,
    encode_position,
)


@dataclass(frozen=True, slots=True)
class CursorParams(PaginationParams):
    """What `PaginationCursor` reads from the query: `limit` and the `cursor` (none for the first
    page), with `after`, the position it points at, already decoded."""

    limit: int = 50
    cursor: str | None = None
    after: Position | None = None
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit starts at 1")

    @property
    def fetch(self) -> int:
        """Rows to query from the database: `limit` and one more, which tells whether there's a
        next page."""
        return self.limit + 1


class CursorPage(LinkedPage, Generic[T]):
    """A page of `items` by cursor; `next_cursor` is None on the last one."""

    items: list[T]
    limit: int = Field(description="Items per page")
    next_cursor: str | None = Field(description="Where the next page starts; None at the end")

    def relations(self) -> list[Relation]:
        cursors: list[tuple[str, str | None]] = [("first", None)]
        if self.next_cursor is not None:
            cursors.append(("next", self.next_cursor))
        return [(rel, {"cursor": cursor, "limit": self.limit}) for rel, cursor in cursors]


class PaginationCursor:
    """Pagination by cursor: `?limit=20`, then `?limit=20&cursor=<next_cursor>`.

        cursors = PaginationCursor(secret=settings.cursor_secret)

        @app.get("/events")
        @cache(expire=60)
        async def events(params: Annotated[CursorParams, Depends(cursors)]) -> CursorPage[Event]:
            rows = ...  # WHERE (created_at, id) > params.after ... LIMIT params.fetch
            return cursors.paginate(rows, params, key=lambda e: (e.created_at, e.id), queried=True)

    A cursor is the sort key of a page's last item, base64-encoded: clients can read it, so sort
    by what they may see. With a `secret` it's signed too (HMAC-SHA256), and a cursor that a
    client made or changed gets a 400 like any other invalid one, before the endpoint runs;
    without it, the values are only checked to be of the types a sort key can have.

    :param secret: at least 32 bytes (`secrets.token_urlsafe(32)`); a list rotates them, the
        first one signing and all of them accepted. None for unsigned cursors.
    :param default_limit: items per page when the request doesn't say.
    :param max_limit: the most a request may ask for (a 422 beyond).
    """

    def __init__(
        self,
        secret: Secrets | None = None,
        *,
        default_limit: int = 50,
        max_limit: int = 100,
    ) -> None:
        check_limits(default_limit, max_limit, "limit")
        self.default_limit = default_limit
        self.max_limit = max_limit
        self._sealer = Sealer(secret, b"fastapi_alcazaba.cursor")
        self.__signature__ = inspect.Signature(
            [
                *REQUEST,
                query(
                    "cursor",
                    str | None,
                    None,
                    max_length=MAX_TOKEN,
                    pattern=SEALED_PATTERN,
                    description="Where the page starts: the next_cursor of the page before",
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
            return_annotation=CursorParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> CursorParams:
        params = self.params(cursor=cursor, limit=limit)
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> CursorParams:
        """The parameters of a page, with the cursor decoded, as the dependency gives them.
        Raises an HTTPException (400) if the cursor isn't valid."""
        limit = self.default_limit if limit is None else limit
        if limit > self.max_limit:
            raise ValueError(f"limit can't be over {self.max_limit}")
        after = None if cursor is None else self.decode(cursor)
        return CursorParams(limit=limit, cursor=cursor, after=after)

    def encode(
        self,
        position: Position,
    ) -> str:
        """The cursor of `position`."""
        return self._sealer.seal(encode_position(position))

    def decode(
        self,
        cursor: str,
    ) -> Position:
        """The position `cursor` points at. Raises an HTTPException (400) if it isn't valid."""
        try:
            return decode_position(self._sealer.open(cursor))
        except ValueError:
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
                except TypeError:  # a cursor of another endpoint, with another kind of key
                    raise HTTPException(400, "Invalid cursor") from None
            page = list(items[start : start + params.limit])
            more = start + params.limit < len(items)
        next_cursor = self.encode(key(page[-1])) if more and page else None
        return linked(
            CursorPage(items=page, limit=params.limit, next_cursor=next_cursor),
            params.links,
        )
