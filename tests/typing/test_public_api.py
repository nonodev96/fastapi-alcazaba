"""Checked by mypy (it's in `files`): the decorator keeps the parameters and the return type.
The `type: ignore` lines must stay errors: with warn_unused_ignores, mypy fails if they stop
being errors."""

from typing import assert_type

from starlette.responses import Response

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.decorator import cache


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


async def _wrong_calls_are_type_errors() -> None:  # never run, only type-checked
    await async_function("1")  # type: ignore[arg-type]
    await async_function(1, unknown=True)  # type: ignore[call-arg]
    sync_function(1).upper()  # type: ignore[attr-defined]
