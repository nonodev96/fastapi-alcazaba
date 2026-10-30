"""Pagination by page token: `?page_size=20`, then `?page_size=20&page_token=<next_page_token>`.
A page token is opaque and signed, and holds where the next page starts, the query it was issued
for and when: a client can't make one up, use it with other filters, or keep it for ever."""

import base64
import hashlib
import inspect
import time
from collections.abc import Sequence
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
from fastapi_alcazaba.pagination.values import MAX_TOKEN, SEALED_PATTERN, Sealer

# The page parameters themselves, which a token is used with whatever their values.
_PAGE_PARAMETERS = frozenset({"page_token", "page_size"})


def _digest(
    pairs: Sequence[tuple[str, str]],
) -> str:
    """The digest of a request's query parameters but `page_token` and `page_size`, in any order:
    what a page token is bound to."""
    kept = sorted((name, value) for name, value in pairs if name not in _PAGE_PARAMETERS)
    digest = hashlib.sha256(urlencode(kept).encode()).digest()[:16]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


_NO_QUERY = _digest(())


@dataclass(frozen=True, slots=True)
class TokenParams(PaginationParams):
    """What `PaginationToken` reads from the query: `page_size` and the `page_token` (none for
    the first page), with `offset`, where the token says the page starts, and `query_digest`,
    the digest of the request's other query parameters, which the next token is bound to. The
    digest is part of the cache key: each query has its own pages, and its own tokens."""

    page_size: int = 50
    offset: int = 0
    page_token: str | None = None
    query_digest: str = _NO_QUERY
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.page_size < 1 or self.offset < 0:
            raise ValueError("page_size starts at 1 and offset at 0")

    @property
    def fetch(self) -> int:
        """Rows to query from the database: `page_size` and one more, which tells whether
        there's a next page."""
        return self.page_size + 1


class TokenPage(LinkedPage, Generic[T]):
    """A page of `items` by page token; `next_page_token` is None on the last one."""

    items: list[T]
    next_page_token: str | None = Field(
        description="Where the next page starts, for the same query; None at the end"
    )

    def relations(self) -> list[Relation]:
        if self.next_page_token is None:
            return []
        return [("next", {"page_token": self.next_page_token})]


class PaginationToken:
    """Pagination by page token: `?page_size=20`, then `?page_size=20&page_token=<token>`.

        tokens = PaginationToken(settings.page_token_secret)

        @app.get("/books")
        async def books(
            params: Annotated[TokenParams, Depends(tokens)],
            genre: Genre | None = None,
        ) -> TokenPage[Book]:
            rows = ...  # WHERE genre = :genre ... LIMIT params.fetch OFFSET params.offset
            return tokens.paginate(rows, params, queried=True)

    A page token holds where the next page starts, a digest of the request's other query
    parameters and when it was issued, signed with HMAC-SHA256. A token that a client made or
    changed, that comes with other query parameters than the ones it was issued for (the next
    page of `genre=poetry` asked with `genre=novel`), or that is older than `ttl`, is a 400
    before the endpoint runs. `page_size` may change from one page to the next. Tokens are
    base64, not encrypted: a client can read the offset in them.

    :param secret: at least 32 bytes (`secrets.token_urlsafe(32)`), the same in every process; a
        list rotates them, the first one signing and all of them accepted.
    :param ttl: seconds a token is accepted for, from when its page was made.
    :param default_page_size: items per page when the request doesn't say.
    :param max_page_size: the most a request may ask for (a 422 beyond).
    """

    def __init__(
        self,
        secret: Secrets,
        *,
        ttl: int = 3600,
        default_page_size: int = 50,
        max_page_size: int = 100,
    ) -> None:
        check_limits(default_page_size, max_page_size, "page_size")
        if ttl < 1:
            raise ValueError("ttl must be at least 1 second")
        self.ttl = ttl
        self.default_page_size = default_page_size
        self.max_page_size = max_page_size
        self._sealer = Sealer(secret, b"fastapi_alcazaba.page_token")
        self.__signature__ = inspect.Signature(
            [
                *REQUEST,
                query_parameter(
                    "page_token",
                    str | None,
                    None,
                    max_length=MAX_TOKEN,
                    pattern=SEALED_PATTERN,
                    description="Where the page starts: the next_page_token of the page before",
                ),
                query_parameter(
                    "page_size",
                    int,
                    default_page_size,
                    ge=1,
                    le=max_page_size,
                    description="Items per page",
                ),
            ],
            return_annotation=TokenParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> TokenParams:
        params = self.params(
            page_token=page_token,
            page_size=page_size,
            query=request.query_params.multi_items(),
        )
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        page_token: str | None = None,
        page_size: int | None = None,
        query: Sequence[tuple[str, str]] = (),
    ) -> TokenParams:
        """The parameters of a page, with the token checked, as the dependency gives them.

        :param query: the request's query parameters, which the token must have been issued
            for; `page_token` and `page_size` among them are left out.

        Raises an HTTPException (400) if the token isn't valid, has expired, or was issued for
        other query parameters.
        """
        page_size = self.default_page_size if page_size is None else page_size
        if page_size > self.max_page_size:
            raise ValueError(f"page_size can't be over {self.max_page_size}")
        digest = _digest(query)
        offset = 0 if page_token is None else self._offset(page_token, digest)
        return TokenParams(
            page_size=page_size,
            offset=offset,
            page_token=page_token,
            query_digest=digest,
        )

    def _offset(
        self,
        page_token: str,
        digest: str,
    ) -> int:
        """Where the page of `page_token` starts, once checked against the query's `digest`."""
        try:
            opened = self._sealer.open(page_token)
        except ValueError:
            raise HTTPException(400, "Invalid page_token") from None
        match opened:
            case [int() as offset, str() as bound_to, int() as issued] if (
                offset >= 0 and not isinstance(offset, bool) and not isinstance(issued, bool)
            ):
                if time.time() - issued > self.ttl:
                    raise HTTPException(400, "Expired page_token")
                if bound_to != digest:
                    raise HTTPException(400, "page_token was issued for other query parameters")
                return offset
        raise HTTPException(400, "Invalid page_token")

    def paginate(
        self,
        items: Sequence[T],
        params: TokenParams,
        *,
        queried: bool = False,
    ) -> TokenPage[T]:
        """The page of `items` that `params` asks for, with the token of the next one, bound to
        the same query parameters.

        :param queried: `items` is what the database returned for this page: up to
            `params.fetch` rows from `params.offset` (`LIMIT :fetch OFFSET :offset`). Otherwise
            `items` is the whole list, and the page is sliced from it.
        """
        if queried:
            rows = list(items)
            page, more = rows[: params.page_size], len(rows) > params.page_size
        else:
            page = list(items[params.offset : params.offset + params.page_size])
            more = params.offset + params.page_size < len(items)
        next_page_token = None
        if more:
            next_offset = params.offset + params.page_size
            payload = [next_offset, params.query_digest, int(time.time())]
            next_page_token = self._sealer.seal(payload)
        return linked(TokenPage(items=page, next_page_token=next_page_token), params.links)
