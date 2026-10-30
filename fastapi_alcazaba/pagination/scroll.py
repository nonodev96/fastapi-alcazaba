"""Pagination by scroll: `?batch_size=100` keeps what the query found as a snapshot in the cache's
backend and answers its first batch, then `?scroll_id=<scroll_id>` the next ones. The batches are
what was found when the scroll started, whatever is written meanwhile: for exports and jobs that
read a whole result set while it changes."""

import hashlib
import inspect
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from itertools import islice
from typing import Generic, TypeVar

from fastapi import HTTPException, Request, Response
from pydantic import Field, TypeAdapter, ValidationError
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.errors import BackendUnavailable, CircuitOpen
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
from fastapi_alcazaba.pagination.values import MAX_TOKEN, SEALED_PATTERN, Sealer
from fastapi_alcazaba.response_cache.caches import Cache, resolve_cache

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

R = TypeVar("R")

_SNAPSHOT = re.compile(r"[A-Za-z0-9_-]{22}")  # secrets.token_urlsafe(16): 128 bits


@dataclass(frozen=True, slots=True)
class ScrollParams(PaginationParams):
    """What `PaginationScroll` reads from the query: `batch_size` and the `scroll_id` (none to
    start a scroll), with what it holds already decoded: the `snapshot` it reads and the
    `offset` where its next batch starts."""

    batch_size: int = 100
    scroll_id: str | None = None
    snapshot: str | None = None
    offset: int = 0
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.batch_size < 1 or self.offset < 0:
            raise ValueError("batch_size starts at 1 and offset at 0")
        if (self.scroll_id is None) != (self.snapshot is None):
            raise ValueError("scroll_id and snapshot go together")


class ScrollPage(LinkedPage, Generic[T]):
    """A batch of `items` of a scroll; `scroll_id` is None after the last one."""

    items: list[T]
    scroll_id: str | None = Field(description="Where the next batch starts; None at the end")
    total: int = Field(description="Items in the snapshot")

    def relations(self) -> list[Relation]:
        if self.scroll_id is None:
            return []
        return [("next", {"scroll_id": self.scroll_id})]


def _digest(*parts: str | None) -> str:
    """SHA-256 of the parts, JSON-encoded so that no two lists of parts give the same text."""
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


class PaginationScroll(Generic[T]):
    """Pagination by scroll: `?batch_size=100` keeps what the query found as a snapshot in the
    cache's backend and answers its first batch, then `?scroll_id=<scroll_id>` the next one.

        scroll = PaginationScroll(Order, secret=settings.scroll_secret)

        @app.get("/orders/export")
        async def export(params: Annotated[ScrollParams, Depends(scroll)]) -> ScrollPage[Order]:
            if params.scroll_id is None:
                return await scroll.start(await find_orders(), params)
            return await scroll.next(params)

    The batches are what was found when the scroll started, whatever is written meanwhile. The
    snapshot lasts `keep_alive` seconds after each batch; once it has expired, its scroll_id is a
    404 and the client starts again. A scroll_id is signed (HMAC-SHA256) and holds the
    snapshot's id and where the next batch starts, so a retried request gets the same batch.

    The snapshot is one entry under `Cache.state_key`, which the cache's `clear()` leaves alone,
    in the shared tier of a TieredBackend, and sealed with the cache's `signing_key` or
    `encryption_key` (the latter when it holds personal data). Whoever has a scroll_id reads the
    rest of the snapshot without the endpoint's query running again: when the results depend on
    who asks, pass `owner` (the user's id) to `start` and `next`, and a scroll_id is only good
    for them. Each batch reads the whole snapshot from the backend and writes it back to renew
    it: keep `max_items` modest.

    :param item_type: the type of the items, which writes them into the snapshot as JSON and
        reads them back (a pydantic model, a dataclass...).
    :param secret: at least 32 bytes (`secrets.token_urlsafe(32)`), the same in every process; a
        list rotates them, the first one signing and all of them accepted.
    :param default_batch_size: items per batch when the request doesn't say.
    :param max_batch_size: the most a request may ask for (a 422 beyond).
    :param keep_alive: seconds the snapshot lasts after its last batch.
    :param max_items: the most a snapshot holds: more is a ValueError at `start`.
    :param cache: the Cache whose backend keeps the snapshots; FastAPICache's by default,
        resolved at each call.
    :param namespace: part of the keys, after the cache's prefix (see `Cache.state_key`).
    """

    def __init__(
        self,
        item_type: type[T],
        secret: Secrets,
        *,
        default_batch_size: int = 100,
        max_batch_size: int = 1000,
        keep_alive: int = 60,
        max_items: int = 10_000,
        cache: Cache | None = None,
        namespace: str = "scroll",
    ) -> None:
        check_limits(default_batch_size, max_batch_size, "batch_size")
        if keep_alive < 1:
            raise ValueError("keep_alive must be at least 1 second")
        if max_items < 1:
            raise ValueError("max_items must be at least 1")
        self.item_type = item_type
        self.default_batch_size = default_batch_size
        self.max_batch_size = max_batch_size
        self.keep_alive = keep_alive
        self.max_items = max_items
        self.cache = cache
        self.namespace = namespace
        self._adapter = TypeAdapter(item_type)
        self._sealer = Sealer(secret, b"fastapi_alcazaba.scroll")
        self.__signature__ = inspect.Signature(
            [
                *REQUEST,
                query(
                    "scroll_id",
                    str | None,
                    None,
                    max_length=MAX_TOKEN,
                    pattern=SEALED_PATTERN,
                    description="Where the next batch starts: the scroll_id of the batch before."
                    " None starts a scroll",
                ),
                query(
                    "batch_size",
                    int,
                    default_batch_size,
                    ge=1,
                    le=max_batch_size,
                    description="Items per batch",
                ),
            ],
            return_annotation=ScrollParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        scroll_id: str | None = None,
        batch_size: int | None = None,
    ) -> ScrollParams:
        params = self.params(scroll_id=scroll_id, batch_size=batch_size)
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        scroll_id: str | None = None,
        batch_size: int | None = None,
    ) -> ScrollParams:
        """The parameters of a batch, with the scroll_id decoded, as the dependency gives them.
        Raises an HTTPException (400) if the scroll_id isn't valid."""
        batch_size = self.default_batch_size if batch_size is None else batch_size
        if batch_size > self.max_batch_size:
            raise ValueError(f"batch_size can't be over {self.max_batch_size}")
        if scroll_id is None:
            return ScrollParams(batch_size=batch_size)
        snapshot, offset = self._decode(scroll_id)
        return ScrollParams(
            batch_size=batch_size,
            scroll_id=scroll_id,
            snapshot=snapshot,
            offset=offset,
        )

    async def start(
        self,
        items: Iterable[T],
        params: ScrollParams,
        *,
        owner: str | None = None,
    ) -> ScrollPage[T]:
        """Keep `items` as the snapshot of a new scroll, for `keep_alive` seconds, and return its
        first batch. When they fit in it, nothing is kept and the scroll is over.

        :param items: what the query found, in order: a list, or rows read as they come. More
            than `max_items` raises ValueError, having read one more at most.
        :param owner: who may read the rest (a user's id): `next` must be given the same.
        """
        if params.scroll_id is not None:
            raise ValueError("these parameters go on with a scroll: call next()")
        found = list(islice(items, self.max_items + 1))
        if len(found) > self.max_items:
            raise ValueError(
                f"more than {self.max_items} items: a scroll keeps them all in the backend "
                "(raise max_items, or narrow the query)"
            )
        batch = found[: params.batch_size]
        if len(found) <= params.batch_size:
            return self._page(batch, "", len(found), len(found), params)
        lines = [
            self._adapter.dump_json(item, by_alias=True, round_trip=True, warnings="error")
            for item in found
        ]
        snapshot = secrets.token_urlsafe(16)
        cache = resolve_cache(self.cache)
        key = self._key(cache, snapshot, owner)
        protector = cache.options.protector
        payload = b"\n".join(lines)  # JSON values have no line breaks of their own
        value = protector.seal(key, payload) if protector is not None else payload
        backend = shared_backend(cache.backend)
        await self._run(cache, "set", lambda: backend.set(key, value, self.keep_alive))
        return self._page(batch, snapshot, params.batch_size, len(found), params)

    async def next(
        self,
        params: ScrollParams,
        *,
        owner: str | None = None,
    ) -> ScrollPage[T]:
        """The batch that `params.scroll_id` points at, with the snapshot's lifetime renewed.
        Raises an HTTPException: 404 when the snapshot has expired (or isn't one of this
        `owner`), 503 when the backend fails."""
        snapshot = params.snapshot
        if snapshot is None:
            raise ValueError("these parameters start a scroll: call start()")
        cache = resolve_cache(self.cache)
        key = self._key(cache, snapshot, owner)
        backend = shared_backend(cache.backend)
        stored = await self._run(cache, "get", lambda: backend.get(key))
        end = params.offset + params.batch_size
        lines = None if stored is None else self._open(cache, key, stored)
        batch = None if lines is None else self._read(key, lines[params.offset : end])
        if stored is None or lines is None or batch is None:
            raise HTTPException(HTTP_404_NOT_FOUND, "Expired or unknown scroll_id")
        # Written back as it was read: it's still sealed for this key.
        await self._run(cache, "set", lambda: backend.set(key, stored, self.keep_alive))
        return self._page(batch, snapshot, end, len(lines), params)

    async def clear(
        self,
        scroll_id: str,
        *,
        owner: str | None = None,
    ) -> bool:
        """Delete the snapshot of `scroll_id` before it expires, when the client is done with
        it; whether it was there. Raises an HTTPException: 400 if the scroll_id isn't valid,
        503 when the backend fails."""
        snapshot, _ = self._decode(scroll_id)
        cache = resolve_cache(self.cache)
        key = self._key(cache, snapshot, owner)
        backend = shared_backend(cache.backend)
        return await self._run(cache, "delete", lambda: backend.clear(key=key)) > 0

    def _decode(
        self,
        scroll_id: str,
    ) -> tuple[str, int]:
        """The snapshot and the offset that `scroll_id` holds. Raises an HTTPException (400) if
        it isn't valid."""
        try:
            decoded = self._sealer.open(scroll_id)
        except ValueError:
            decoded = None
        if isinstance(decoded, list) and len(decoded) == 2:
            snapshot, offset = decoded
            if isinstance(snapshot, str) and _SNAPSHOT.fullmatch(snapshot):
                if type(offset) is int and 0 < offset <= self.max_items:
                    return snapshot, offset
        raise HTTPException(HTTP_400_BAD_REQUEST, "Invalid scroll_id")

    def _key(
        self,
        cache: Cache,
        snapshot: str,
        owner: str | None,
    ) -> str:
        return cache.state_key(self.namespace, _digest(snapshot, owner))

    def _open(
        self,
        cache: Cache,
        key: str,
        stored: bytes,
    ) -> list[bytes] | None:
        """The items of the snapshot as JSON, one line each; None if we didn't store it."""
        protector = cache.options.protector
        payload = stored if protector is None else protector.open(key, stored)
        if payload is None:
            logger.warning("Ignoring the snapshot %r: it isn't sealed by us for this key", key)
            return None
        return payload.split(b"\n")

    def _read(
        self,
        key: str,
        lines: list[bytes],
    ) -> list[T] | None:
        """The items of a batch; None if they aren't items of this scroll."""
        try:
            return [self._adapter.validate_json(line) for line in lines]
        except ValidationError as exc:
            logger.warning(
                "Ignoring the snapshot %r: its items aren't %s (kept by another version of the "
                "app, or by another scroll in the same namespace)",
                key,
                getattr(self.item_type, "__name__", self.item_type),
                exc_info=exc,
            )
            return None

    def _page(
        self,
        items: list[T],
        snapshot: str,
        end: int,
        total: int,
        params: ScrollParams,
    ) -> ScrollPage[T]:
        scroll_id = self._sealer.seal([snapshot, end]) if end < total else None
        return linked(ScrollPage(items=items, scroll_id=scroll_id, total=total), params.links)

    @staticmethod
    async def _run(
        cache: Cache,
        operation: str,
        call: Callable[[], Awaitable[R]],
    ) -> R:
        """`call` through the cache's guard (timeout and circuit breaker). Raises an
        HTTPException (503) when the backend fails: the client retries the same request."""
        try:
            return await cache.guard.run(operation, call)
        except BackendUnavailable as exc:
            if isinstance(exc, CircuitOpen):
                logger.debug("Scroll not served: %s", exc)
            else:
                logger.warning("Scroll not served: the backend failed", exc_info=exc)
            raise HTTPException(HTTP_503_SERVICE_UNAVAILABLE) from exc
