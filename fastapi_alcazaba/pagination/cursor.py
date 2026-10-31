"""Pagination by cursor, in both directions: `?limit=20`, then `?cursor=<next_cursor>&limit=20`
for the next page or `?cursor=<prev_cursor>&limit=20` for the one before. A cursor is opaque and
signed: it holds the sort key of the item at the edge of a page, the query it was issued for and
when. A database finds the page through an index from either side, rows added or deleted
meanwhile don't shift the pages, and a client can't make a cursor up, use it with other filters
or, with a `ttl`, keep it for ever."""

import base64
import hashlib
import inspect
import operator
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Generic
from urllib.parse import urlencode

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
)
from fastapi_alcazaba.pagination.common import query as query_parameter
from fastapi_alcazaba.pagination.values import (
    MAX_TOKEN,
    SEALED_PATTERN,
    Position,
    Sealer,
    decode_position,
    encode_position,
)

# The page parameters themselves, which a cursor is used with whatever their values.
_PAGE_PARAMETERS = frozenset({"cursor", "limit"})


def _digest(
    pairs: Sequence[tuple[str, str]],
) -> str:
    """The digest of a request's query parameters but `cursor` and `limit`, in any order: what a
    cursor is bound to."""
    kept = sorted((name, value) for name, value in pairs if name not in _PAGE_PARAMETERS)
    digest = hashlib.sha256(urlencode(kept).encode()).digest()[:16]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


_NO_QUERY = _digest(())


@dataclass(frozen=True, slots=True)
class CursorParams(PaginationParams):
    """What `PaginationCursor` reads from the query: `limit` and the `cursor` (none for the first
    page), with the position it points at already decoded: `after` for a next page, `before`
    for a previous one. `query_digest` is the digest of the request's other query parameters,
    which the page's cursors are bound to; it's part of the cache key, so that each query has its
    own pages and its own cursors."""

    limit: int = 50
    cursor: str | None = None
    after: Position | None = None
    before: Position | None = None
    query_digest: str = _NO_QUERY
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit starts at 1")
        if self.after is not None and self.before is not None:
            raise ValueError("a page is after a position or before one, not both")

    @property
    def fetch(self) -> int:
        """Rows to query from the database: `limit` and one more, which tells whether there's
        another page in the direction of the query."""
        return self.limit + 1


class CursorPage(LinkedPage, Generic[T]):
    """A page of `items` by cursor: `prev_cursor` asks for the page before it (None on the
    first), `next_cursor` for the next (None on the last)."""

    items: list[T]
    limit: int = Field(description="Items per page")
    prev_cursor: str | None = Field(description="Where the page before ends; None on the first")
    next_cursor: str | None = Field(description="Where the next page starts; None on the last")

    def relations(self) -> list[Relation]:
        cursors: list[tuple[str, str | None]] = [("first", None)]
        if self.prev_cursor is not None:
            cursors.append(("prev", self.prev_cursor))
        if self.next_cursor is not None:
            cursors.append(("next", self.next_cursor))
        return [(rel, {"cursor": cursor, "limit": self.limit}) for rel, cursor in cursors]


def _past(
    value: Position,
    position: Position,
    descending: bool,
) -> bool:
    """Whether `value` comes after `position` in the list's order. Raises TypeError when they
    can't be compared."""
    return bool(operator.lt(value, position) if descending else operator.lt(position, value))


class PaginationCursor:
    """Pagination by cursor, in both directions: `?limit=20`, then `?cursor=<next_cursor>` for
    the next page or `?cursor=<prev_cursor>` for the one before.

        cursors = PaginationCursor(settings.cursor_secret)

        @app.get("/events")
        @cache(expire=60)
        async def events(params: Annotated[CursorParams, Depends(cursors)]) -> CursorPage[Event]:
            rows = ...  # WHERE (created_at, id) > params.after ORDER BY ... LIMIT params.fetch,
            # or WHERE (created_at, id) < params.before ORDER BY ... DESC LIMIT params.fetch
            return cursors.paginate(rows, params, key=lambda e: (e.created_at, e.id), queried=True)

    A cursor holds the sort key of the first or last item of a page, a digest of the request's
    other query parameters and when it was issued, signed with HMAC-SHA256. A cursor that a
    client made or changed, that comes with other query parameters than the ones it was issued
    for (the next page of `genre=poetry` asked with `genre=novel`), or that is older than `ttl`,
    is a 400 before the endpoint runs. `limit` may change from one page to the next. Cursors are
    base64, not encrypted: a client can read the sort key in them, so sort by what it may see.

    :param secret: at least 32 bytes (`secrets.token_urlsafe(32)`), the same in every process; a
        list rotates them, the first one signing and all of them accepted.
    :param ttl: seconds a cursor is accepted for, from when its page was made; None for ever.
    :param default_limit: items per page when the request doesn't say.
    :param max_limit: the most a request may ask for (a 422 beyond).
    """

    def __init__(
        self,
        secret: Secrets,
        *,
        ttl: int | None = None,
        default_limit: int = 50,
        max_limit: int = 100,
    ) -> None:
        check_limits(default_limit, max_limit, "limit")
        if ttl is not None and ttl < 1:
            raise ValueError("ttl must be at least 1 second, or None")
        self.ttl = ttl
        self.default_limit = default_limit
        self.max_limit = max_limit
        self._sealer = Sealer(secret, b"fastapi_alcazaba.cursor")
        self.__signature__ = inspect.Signature(
            [
                *REQUEST,
                query_parameter(
                    "cursor",
                    str | None,
                    None,
                    max_length=MAX_TOKEN,
                    pattern=SEALED_PATTERN,
                    description="Where the page is: the next_cursor or prev_cursor of a page",
                ),
                query_parameter(
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
        params = self.params(
            cursor=cursor,
            limit=limit,
            query=request.query_params.multi_items(),
        )
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        query: Sequence[tuple[str, str]] = (),
    ) -> CursorParams:
        """The parameters of a page, with the cursor checked and decoded, as the dependency
        gives them.

        :param query: the request's query parameters, which the cursor must have been issued
            for; `cursor` and `limit` among them are left out.

        Raises an HTTPException (400) if the cursor isn't valid, has expired, or was issued for
        other query parameters.
        """
        limit = self.default_limit if limit is None else limit
        if limit > self.max_limit:
            raise ValueError(f"limit can't be over {self.max_limit}")
        digest = _digest(query)
        after = before = None
        if cursor is not None:
            forward, position = self._open(cursor, digest)
            after, before = (position, None) if forward else (None, position)
        return CursorParams(
            limit=limit,
            cursor=cursor,
            after=after,
            before=before,
            query_digest=digest,
        )

    def encode(
        self,
        position: Position,
        *,
        before: bool = False,
        query: Sequence[tuple[str, str]] = (),
    ) -> str:
        """The cursor of the page after `position` (before it, with `before`), for requests with
        these other query parameters: for links of your own, such as the page that starts at a
        date."""
        return self._seal(not before, position, _digest(query))

    def _seal(
        self,
        forward: bool,
        position: Position,
        digest: str,
    ) -> str:
        # When it was issued, if it expires: without a ttl a page always has the same cursors.
        issued = int(time.time()) if self.ttl is not None else 0
        return self._sealer.seal(
            ["a" if forward else "b", encode_position(position), digest, issued]
        )

    def _open(
        self,
        cursor: str,
        digest: str,
    ) -> tuple[bool, Position]:
        """(whether it points forward, its position), once checked against the query's
        `digest` and the `ttl`."""
        try:
            opened = self._sealer.open(cursor)
            match opened:
                case ["a" | "b" as way, position, str() as bound_to, int() as issued] if (
                    not isinstance(issued, bool)
                ):
                    decoded = decode_position(position)
                case _:
                    raise ValueError("not a cursor")
        except ValueError:
            raise HTTPException(400, "Invalid cursor") from None
        if self.ttl is not None and time.time() - issued > self.ttl:
            raise HTTPException(400, "Expired cursor")
        if bound_to != digest:
            raise HTTPException(400, "cursor was issued for other query parameters")
        return way == "a", decoded

    def paginate(
        self,
        items: Sequence[T],
        params: CursorParams,
        *,
        key: Callable[[T], Position],
        descending: bool = False,
        queried: bool = False,
    ) -> CursorPage[T]:
        """The page of `items` that `params` asks for, in the list's order whichever the
        direction, with the cursors of the pages around it, bound to the same query parameters.

        :param key: an item's sort key: a unique value or tuple (`(e.created_at, e.id)`, not
            `e.created_at` alone, which two items may share).
        :param descending: the list goes from the greatest key to the smallest (newest first).
        :param queried: `items` is what the database returned for this page: up to
            `params.fetch` rows past `params.after`, in the list's order; or with
            `params.before`, the rows before it in the reverse order, nearest first (`ORDER BY
            key DESC` for an ascending list), which `paginate` puts back in order. The extra row
            tells whether there's another page that way; the other way, a page reached by a
            cursor takes there to be one, the page it came from. Otherwise `items` is the whole
            list, sorted by `key`, and the page is found in it.
        """
        if queried:
            rows = list(items)
            more = len(rows) > params.limit
            if params.before is not None:
                page = rows[: params.limit][::-1]
                earlier, later = more, True
            else:
                page = rows[: params.limit]
                earlier, later = params.after is not None, more
        else:
            start, stop = self._window(items, params, key, descending)
            page = list(items[start:stop])
            earlier, later = start > 0, stop < len(items)
        digest = params.query_digest
        prev_cursor = self._seal(False, key(page[0]), digest) if earlier and page else None
        next_cursor = self._seal(True, key(page[-1]), digest) if later and page else None
        return linked(
            CursorPage(
                items=page,
                limit=params.limit,
                prev_cursor=prev_cursor,
                next_cursor=next_cursor,
            ),
            params.links,
        )

    @staticmethod
    def _window(
        items: Sequence[T],
        params: CursorParams,
        key: Callable[[T], Position],
        descending: bool,
    ) -> tuple[int, int]:
        """Where the page starts and stops in the whole list."""
        try:
            if params.before is not None:
                before = params.before
                stop = next(
                    (i for i, item in enumerate(items) if not _past(before, key(item), descending)),
                    len(items),
                )
                return max(stop - params.limit, 0), stop
            after = params.after
            start = (
                0
                if after is None
                else next(
                    (i for i, item in enumerate(items) if _past(key(item), after, descending)),
                    len(items),
                )
            )
        except TypeError:  # a cursor of another endpoint, with another kind of sort key
            raise HTTPException(400, "Invalid cursor") from None
        return start, start + params.limit
