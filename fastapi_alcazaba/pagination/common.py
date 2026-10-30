"""What the pagination strategies share: the base of their parameters and pages, the Link header
(RFC 8288) that a page carries, and the query parameters of their dependencies."""

import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar
from urllib.parse import quote, urlencode

from fastapi import Query, Request, Response
from pydantic import BaseModel

T = TypeVar("T")

# What a path keeps unquoted in a link: no "<", ">", "," or ";", which end it or the link.
_PATH_SAFE = "/:@!$&'()*+="

#: A link to another page: its relation ("next"...) and the page parameters that reach it, None
#: leaving one out of the URL.
Relation = tuple[str, dict[str, object]]


class LinkedPage(BaseModel):
    """The base of the pages: `relations()` says which pages the Link header points at, and
    `headers()` what else the response carries (`Content-Range`...)."""

    def relations(self) -> list[Relation]:
        """The pages around this one, by relation, as their parameters; none by default."""
        return []

    def headers(self) -> dict[str, str]:
        """Headers of the response besides Link; none by default."""
        return {}


@dataclass(frozen=True, slots=True)
class PageLinks:
    """Where the Link header of a page goes: the response to the request that asked for it, and
    the request's path and query parameters. The links keep the other parameters (filters,
    sorting) and are relative to the host (`</products?page=3&page_size=20>`), so they don't
    depend on the Host header nor on whether a proxy in front speaks HTTPS."""

    response: Response
    path: str
    query: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, request: Request, response: Response) -> "PageLinks":
        # The path as decoded and quoted again: nothing from the client ends the link early.
        path = quote(request.scope["path"], safe=_PATH_SAFE)
        return cls(response, path, tuple(request.query_params.multi_items()))

    def add(self, page: object) -> None:
        """Add the links of `page` (its `relations()`) to the response's Link header, and its
        other `headers()`."""
        if not isinstance(page, LinkedPage):
            return
        self.response.headers.update(page.headers())
        links = [self._link(rel, values) for rel, values in page.relations()]
        if not links:
            return
        existing = self.response.headers.get("link")
        self.response.headers["Link"] = ", ".join([existing, *links] if existing else links)

    def _link(self, rel: str, values: dict[str, object]) -> str:
        """The link to the request's URL with `values` as page parameters (None leaves one out)."""
        pairs = [(name, value) for name, value in self.query if name not in values]
        pairs += [(name, str(value)) for name, value in values.items() if value is not None]
        return f'<{self.path}?{urlencode(pairs)}>; rel="{rel}"'


class PaginationParams:
    """The base of what each strategy reads from the query. The cache looks for it among the
    endpoint's arguments, to give a page served from it the Link header of the request it
    answers."""

    __slots__ = ()

    #: Where `paginate` puts the Link header; None outside a request (`params()`).
    links: PageLinks | None


PageT = TypeVar("PageT", bound=LinkedPage)


def linked(page: PageT, links: PageLinks | None) -> PageT:
    """`page`, with its links added to the response when there is one."""
    if links is not None:
        links.add(page)
    return page


def check_limits(
    default: int,
    maximum: int,
    name: str,
) -> None:
    if not 1 <= default <= maximum:
        raise ValueError(f"the default {name} must be between 1 and the maximum")


def query(
    name: str,
    kind: Any,
    default: Any,
    **constraints: Any,
) -> inspect.Parameter:
    """A query parameter of a dependency's signature, with FastAPI's `Query` constraints."""
    return inspect.Parameter(
        name,
        inspect.Parameter.KEYWORD_ONLY,
        default=default,
        annotation=Annotated[kind, Query(**constraints)],
    )


def window(
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


#: The request and FastAPI's response, for the links: no query parameters, nothing in OpenAPI.
REQUEST = [
    inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY, annotation=Request),
    inspect.Parameter("response", inspect.Parameter.KEYWORD_ONLY, annotation=Response),
]
