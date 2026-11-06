"""Checked by mypy (it's in `files`): the decorator keeps the parameters and the return type.
The `type: ignore` lines must stay errors: with warn_unused_ignores, mypy fails if they stop
being errors."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, assert_type

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response

from fastapi_alcazaba import Cache, FastAPIBreaker, FastAPICache, FastAPIScheduler
from fastapi_alcazaba.auth import (
    FastAPIPasswords,
    PasswordBlocklist,
    PasswordCheck,
    PasswordStrength,
    RefreshTokens,
    TokenBlocklist,
    TokenPair,
)
from fastapi_alcazaba.auth.strategies.jwt import JWTKey, JWTStrategy
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.background import FastAPITaskQueue, TaskStatus
from fastapi_alcazaba.countries import Country, CountryCode, country
from fastapi_alcazaba.countries.spain import (
    NIF,
    Province,
    ProvinceCode,
    SpanishId,
    SpanishIdKind,
    parse_spanish_id,
    province,
)
from fastapi_alcazaba.pagination.cursor import CursorPage, PaginationCursor
from fastapi_alcazaba.pagination.keyset import KeysetPage, PaginationKeyset
from fastapi_alcazaba.pagination.offset import OffsetPage, PaginationOffset
from fastapi_alcazaba.pagination.page_number import PageNumberPage, PaginationPageNumber
from fastapi_alcazaba.rate_limit import FastAPIRateLimiter, client_network
from fastapi_alcazaba.response_cache import cache


@cache(expire=60)
async def async_function(x: int, *, name: str = "") -> str:
    return f"{name}{x}"


@cache(expire=60)
def sync_function(x: int) -> int:
    return x


@cache(expire=60, cache_control="public, s-maxage=600")
async def shared_function(x: int) -> str:
    return str(x)


def _cache_control_is_directives() -> None:
    """Never called: only mypy reads it."""
    cache(cache_control=600)  # type: ignore[arg-type]  # directives, not a number


async def test_types_are_preserved() -> None:
    FastAPICache.init(InMemoryBackend())
    assert_type(await async_function(1, name="a"), str | Response)
    assert_type(await sync_function(2), int | Response)
    assert_type(await shared_function(3), str | Response)
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
    pages, offsets, cursors, keyset = (
        PaginationPageNumber(),
        PaginationOffset(),
        PaginationCursor("s" * 32),
        PaginationKeyset(sort=str),
    )
    assert_type(pages.paginate([1, 2, 3], pages.params()), PageNumberPage[int])
    assert_type(offsets.paginate(["a"], offsets.params(), total=10), OffsetPage[str])
    assert_type(cursors.paginate(["a"], cursors.params(), key=str.upper), CursorPage[str])
    assert_type(keyset.paginate(["a"], keyset.params(), key=str.upper), KeysetPage[str])
    assert pages.paginate([1, 2, 3], pages.params(page_size=2)).items == [1, 2]


async def test_refresh_token_types() -> None:
    strategy = JWTStrategy("j" * 32, blocklist=TokenBlocklist(Cache(InMemoryBackend())))
    refresh = RefreshTokens(strategy, secret="r" * 32, cache=Cache(InMemoryBackend()))
    pair = await refresh.issue("ana", client="ios", scope="read")
    assert_type(pair, TokenPair)
    assert_type(await refresh.exchange(pair.refresh_token, client="ios"), TokenPair)
    assert_type(await refresh.revoke(pair.refresh_token), bool)


async def _task_queue_types() -> None:  # never run (no Redis), only type-checked
    queue = FastAPITaskQueue(Cache(InMemoryBackend()))

    @queue.task(timeout=5, retry_on=ConnectionError, give_up_on=(ValueError,))
    async def job(n: int) -> int:
        return n

    assert_type(await job(1), int)  # the decorator keeps the function's type
    assert_type(await queue.enqueue(job, 1, unique_key="k"), str)
    assert_type(await queue.status("a" * 32), TaskStatus | None)
    assert_type(await queue.cancel("a" * 32), bool)
    assert_type(await queue.dead_letter_count(), int)
    queue.task(timeout="5")  # type: ignore[arg-type]
    await queue.enqueue(job, 1, unique_key=1)  # type: ignore[arg-type]


async def test_jwt_key_types() -> None:
    strategy = JWTStrategy(keys=[JWTKey("k" * 32, algorithm="HS256", kid="k1")])
    assert_type(strategy.keys, list[JWTKey])
    assert_type(strategy.keys[0].kid, str)
    assert_type(strategy.jwks(), dict[str, list[dict[str, Any]]])
    assert_type(await strategy.decode(strategy.issue("ana")), dict[str, Any])
    await strategy.aclose()


class Customer(BaseModel):
    country: CountryCode
    nif: NIF
    province: ProvinceCode


def test_country_types() -> None:
    assert_type(country("ES"), Country)
    assert_type(country(724).alpha_3, str)
    assert_type(country("ES").official_name, str | None)
    assert_type(province(28), Province)
    assert_type(parse_spanish_id("12345678Z"), SpanishId)
    assert_type(parse_spanish_id("12345678Z", kinds={SpanishIdKind.DNI}).kind, SpanishIdKind)
    customer = Customer(country="es", nif="12345678z", province=ProvinceCode.MADRID)
    assert_type(customer.country, str)
    assert_type(customer.nif, str)
    assert customer.nif == "12345678Z"


async def test_password_check_types() -> None:
    passwords = FastAPIPasswords(blocklist=PasswordBlocklist(["acme2024"]))
    result = await passwords.evaluate("a long passphrase", user_inputs=["ana@example.com"])
    assert_type(result, PasswordCheck)
    assert_type(result.problems, tuple[str, ...])
    assert_type(result.score, int | None)
    assert_type(result.ok, bool)
    assert_type(await passwords.check("a long passphrase"), list[str])


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
    PaginationCursor("s" * 32).paginate([1], PaginationPageNumber().params(), key=str)  # type: ignore[arg-type]
    PaginationCursor()  # type: ignore[call-arg]  # pylint: disable=no-value-for-parameter
    refresh = RefreshTokens(JWTStrategy("j" * 32), secret="r" * 32)
    await refresh.issue(1)  # type: ignore[arg-type]
    await refresh.exchange("token", client=1)  # type: ignore[arg-type]
    RefreshTokens(JWTStrategy("j" * 32), secret="r" * 32, grace="10")  # type: ignore[arg-type]
    JWTKey("k" * 32)  # type: ignore[call-arg]  # pylint: disable=missing-kwoa
    JWTStrategy(keys=[JWTKey("k" * 32, algorithm="HS256")], jwks_stale="1")  # type: ignore[arg-type]
    country(None)  # type: ignore[arg-type]
    parse_spanish_id(12345678)  # type: ignore[arg-type]
    parse_spanish_id("12345678Z", kinds="DNI")  # type: ignore[arg-type]
    province(ProvinceCode.MADRID).community.provinces.append(None)  # type: ignore[attr-defined]
    await FastAPIPasswords().evaluate(b"bytes")  # type: ignore[arg-type]
    PasswordStrength(min_score="3")  # type: ignore[arg-type]
    PasswordBlocklist([1])  # type: ignore[list-item]
