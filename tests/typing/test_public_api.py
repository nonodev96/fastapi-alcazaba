"""Checked by mypy (it's in `files`): the decorator keeps the parameters and the return type.
The `type: ignore` lines must stay errors: with warn_unused_ignores, mypy fails if they stop
being errors."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, assert_type

from starlette.requests import Request
from starlette.responses import Response

from fastapi_alcazaba import Cache, FastAPIBreaker, FastAPICache, FastAPIScheduler
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.cursor import CursorPage, PaginationCursor
from fastapi_alcazaba.pagination.limit_offset import LimitOffsetPage, PaginationLimitOffset
from fastapi_alcazaba.pagination.page_number import PageNumberPage, PaginationPageNumber
from fastapi_alcazaba.rate_limit import FastAPIRateLimiter, client_network
from fastapi_alcazaba.response_cache import cache


@cache(expire=60)
async def async_function(x: int, *, name: str = "") -> str:
    return f"{name}{x}"


@cache(expire=60)
def sync_function(x: int) -> int:
    return x


async def test_types_are_preserved() -> None:
    FastAPICache.init(InMemoryBackend())
    assert_type(await async_function(1, name="a"), str | Response)
    assert_type(await sync_function(2), int | Response)
    assert await async_function(1, name="a") == "a1"
    assert await sync_function(2) == 2


async def test_rate_limiter_types() -> None:
    limiter = FastAPIRateLimiter(times=2, seconds=1, cache=Cache(InMemoryBackend()))
    request = Request({"type": "http", "path": "/", "client": None})
    assert_type(await limiter.hit(request), float | None)
    assert_type(await limiter.hit(request, key="message", cost=2), float | None)
    assert_type(await limiter(request, Response()), None)
    assert_type(client_network(24, 48)(request), Awaitable[str] | str)


breaker = FastAPIBreaker("dependency")
scheduler = FastAPIScheduler()


@breaker
async def guarded_async(x: int) -> str:
    return str(x)


@breaker
def guarded_sync(x: int) -> int:
    return x


@scheduler.every(minutes=5, exclusive=False)
async def every_five_minutes() -> None:
    pass


async def test_breaker_and_scheduler_types() -> None:
    assert_type(await guarded_async(1), str)
    assert_type(asyncio.create_task(guarded_async(1)), asyncio.Task[str])
    assert_type(guarded_sync(2), int)
    assert_type(every_five_minutes, Callable[[], Coroutine[Any, Any, None]])
    assert await guarded_async(1) == "1"
    assert guarded_sync(2) == 2


def test_pagination_types() -> None:
    pages, offsets, cursors = (
        PaginationPageNumber(),
        PaginationLimitOffset(),
        PaginationCursor(),
    )
    assert_type(pages.paginate([1, 2, 3], pages.params()), PageNumberPage[int])
    assert_type(offsets.paginate(["a"], offsets.params(), total=10), LimitOffsetPage[str])
    assert_type(cursors.paginate(["a"], cursors.params(), key=str.upper), CursorPage[str])
    assert pages.paginate([1, 2, 3], pages.params(page_size=2)).items == [1, 2]


async def _wrong_calls_are_type_errors() -> None:  # never run, only type-checked
    # pylint: disable=no-member  # wrong on purpose: mypy must refuse each of these calls
    await async_function("1")  # type: ignore[arg-type]
    await async_function(1, unknown=True)  # type: ignore[call-arg]
    sync_function(1).upper()  # type: ignore[attr-defined]
    FastAPIRateLimiter(times="10", minutes=1)  # type: ignore[arg-type]
    FastAPIRateLimiter(10, minutes=1, identifier=lambda connection: 1)  # type: ignore[arg-type,return-value]
    FastAPIRateLimiter(10, minutes=1, cost="2")  # type: ignore[arg-type]
    await guarded_async("1")  # type: ignore[arg-type]
    guarded_sync(1).upper()  # type: ignore[attr-defined]
    scheduler.every(minutes="5")  # type: ignore[arg-type]
    PaginationPageNumber().paginate([1], {"page": 1})  # type: ignore[arg-type]
    PaginationCursor().paginate([1], PaginationPageNumber().params(), key=str)  # type: ignore[arg-type]
