"""Pagination by keyset, in both directions: `?limit=20`, then `?after=<after>&limit=20` for the
next page or `?before=<before>&limit=20` for the one before. A key is the sort key of the item at
the edge of a page, sealed into a token: a database finds the page through an index from either
side, and rows added or deleted meanwhile don't shift the pages."""

import inspect
import operator
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
class KeysetParams(PaginationParams):
    """What `PaginationKeyset` reads from the query: `limit`, and the position of `after` or of
    `before`, decoded (neither on the first page; never both)."""

    limit: int = 50
    after: Position | None = None
    before: Position | None = None
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit starts at 1")
        if self.after is not None and self.before is not None:
            raise ValueError("a page is after a key or before one, not both")

    @property
    def fetch(self) -> int:
        """Rows to query from the database: `limit` and one more, which tells whether there's
        another page in the direction of the query."""
        return self.limit + 1


class KeysetPage(LinkedPage, Generic[T]):
    """A page of `items` by keyset: `before` asks for the page before it, `after` for the next."""

    items: list[T]
    limit: int = Field(description="Items per page")
    before: str | None = Field(
        description="Where the page before ends: the key of the first item; None on the first page",
    )
    after: str | None = Field(
        description="Where the next page starts: the key of the last item; None on the last page",
    )

    def relations(self) -> list[Relation]:
        keys: list[tuple[str, str | None, str | None]] = [("first", None, None)]
        if self.before is not None:
            keys.append(("prev", None, self.before))
        if self.after is not None:
            keys.append(("next", self.after, None))
        return [
            (rel, {"after": after, "before": before, "limit": self.limit})
            for rel, after, before in keys
        ]


def _past(
    value: Position,
    position: Position,
    descending: bool,
) -> bool:
    """Whether `value` comes after `position` in the list's order. Raises TypeError when they
    can't be compared."""
    return bool(operator.lt(value, position) if descending else operator.lt(position, value))


class PaginationKeyset:
    """Pagination by keyset, in both directions: `?limit=20`, then `?after=<after>` for the next
    page or `?before=<before>` for the one before.

        keyset = PaginationKeyset(secret=settings.keyset_secret)

        @app.get("/products")
        @cache(expire=60)
        async def products(
            params: Annotated[KeysetParams, Depends(keyset)],
        ) -> KeysetPage[Product]:
            rows = ...  # WHERE (price, id) > params.after ORDER BY price, id LIMIT params.fetch,
            # or WHERE (price, id) < params.before ORDER BY price DESC, id DESC LIMIT params.fetch
            return keyset.paginate(rows, params, key=lambda p: (p.price, p.id), queried=True)

    A key is the sort key of the first or last item of a page, base64-encoded: clients can read
    it, so sort by what they may see. With a `secret` it's signed too (HMAC-SHA256), and a key
    that a client made or changed gets a 400 like any other invalid one, before the endpoint
    runs; without it, the values are only checked to be of the types a sort key can have.

    :param secret: at least 32 bytes (`secrets.token_urlsafe(32)`); a list rotates them, the
        first one signing and all of them accepted. None for unsigned keys.
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
        self._sealer = Sealer(secret, b"fastapi_alcazaba.keyset")
        self.__signature__ = inspect.Signature(
            [
                *REQUEST,
                query(
                    "after",
                    str | None,
                    None,
                    max_length=MAX_TOKEN,
                    pattern=SEALED_PATTERN,
                    description="The page after this key: the after of the page before",
                ),
                query(
                    "before",
                    str | None,
                    None,
                    max_length=MAX_TOKEN,
                    pattern=SEALED_PATTERN,
                    description="The page before this key: the before of the page after",
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
        before: str | None = None,
        limit: int | None = None,
    ) -> KeysetParams:
        params = self.params(after=after, before=before, limit=limit)
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        after: str | None = None,
        before: str | None = None,
        limit: int | None = None,
    ) -> KeysetParams:
        """The parameters of a page, with its key decoded, as the dependency gives them. Raises
        an HTTPException (400) if a key isn't valid, or if both are given."""
        limit = self.default_limit if limit is None else limit
        if limit > self.max_limit:
            raise ValueError(f"limit can't be over {self.max_limit}")
        if after is not None and before is not None:
            raise HTTPException(400, "Give after or before, not both")
        return KeysetParams(
            limit=limit,
            after=None if after is None else self.decode(after, name="after"),
            before=None if before is None else self.decode(before, name="before"),
        )

    def encode(
        self,
        position: Position,
    ) -> str:
        """The key of `position`."""
        return self._sealer.seal(encode_position(position))

    def decode(
        self,
        key: str,
        *,
        name: str = "after",
    ) -> Position:
        """The position `key` points at. Raises an HTTPException (400, "Invalid <name>") if it
        isn't valid."""
        try:
            return decode_position(self._sealer.open(key))
        except ValueError:
            raise HTTPException(400, f"Invalid {name}") from None

    def paginate(
        self,
        items: Sequence[T],
        params: KeysetParams,
        *,
        key: Callable[[T], Position],
        descending: bool = False,
        queried: bool = False,
    ) -> KeysetPage[T]:
        """The page of `items` that `params` asks for, in the list's order whichever the
        direction.

        :param key: an item's sort key: a unique value or tuple (`(p.price, p.id)`, not
            `p.price` alone, which two items may share).
        :param descending: the list goes from the greatest key to the smallest (newest first).
        :param queried: `items` is what the database returned for this page: up to
            `params.fetch` rows past `params.after`, in the list's order; or with
            `params.before`, the rows before it in the reverse order, nearest first (`ORDER BY
            key DESC` for an ascending list), which `paginate` puts back in order. The extra row
            tells whether there's another page that way; the other way, a page reached by a key
            takes there to be one, the page it came from. Otherwise `items` is the whole list,
            sorted by `key`, and the page is found in it.
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
        before = self.encode(key(page[0])) if earlier and page else None
        after = self.encode(key(page[-1])) if later and page else None
        return linked(
            KeysetPage(items=page, limit=params.limit, before=before, after=after),
            params.links,
        )

    @staticmethod
    def _window(
        items: Sequence[T],
        params: KeysetParams,
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
        except TypeError:  # a key of another endpoint, with another kind of sort key
            name = "before" if params.before is not None else "after"
            raise HTTPException(400, f"Invalid {name}") from None
        return start, start + params.limit
