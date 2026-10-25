"""Performance of the hot paths. Run with `pytest tests/benchmarks --benchmark-only`; CI keeps the
results as an artifact to compare releases."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel
from pytest_benchmark.fixture import BenchmarkFixture
from starlette.requests import Request

from fastapi_alcazaba import Cache, FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.coder import JsonCoder
from fastapi_alcazaba.decorator import cache
from fastapi_alcazaba.encryption import Encrypter
from fastapi_alcazaba.integrity import Signer
from fastapi_alcazaba.key_builder import default_key_builder
from fastapi_alcazaba.limiter import FastAPIRateLimiter


class Item(BaseModel):
    name: str
    price: float
    tags: list[str]


ITEMS = [Item(name=f"item {i}", price=i * 1.5, tags=["a", "b"]) for i in range(100)]


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.mark.parametrize(
    "protection", ["plain", "signed", "encrypted", "zstd", "zlib", "encrypted+zstd"]
)
def test_cache_hit(
    benchmark: BenchmarkFixture, loop: asyncio.AbstractEventLoop, protection: str
) -> None:
    key = "k" * 32
    codec = protection.rpartition("+")[2]
    FastAPICache.init(
        InMemoryBackend(),
        signing_key=key if protection == "signed" else None,
        encryption_key=key if protection.startswith("encrypted") else None,
        compression="zstd" if codec == "zstd" else "zlib" if codec == "zlib" else None,
    )

    @cache(expire=60)
    async def items() -> list[Item]:
        return ITEMS

    loop.run_until_complete(items())  # fill the cache
    result: Any = benchmark(lambda: loop.run_until_complete(items()))
    assert result == ITEMS


def test_uncached_call(benchmark: BenchmarkFixture, loop: asyncio.AbstractEventLoop) -> None:
    """Baseline for test_cache_hit: the same function without the cache."""

    async def items() -> list[Item]:
        return ITEMS

    benchmark(lambda: loop.run_until_complete(items()))


def test_json_encode(benchmark: BenchmarkFixture) -> None:
    benchmark(JsonCoder.encode, ITEMS)


def test_json_decode_as_type(benchmark: BenchmarkFixture) -> None:
    payload = JsonCoder.encode(ITEMS)
    result = benchmark(JsonCoder.decode_as_type, payload, type_=list[Item])
    assert result == ITEMS


def test_default_key_builder(benchmark: BenchmarkFixture) -> None:
    def endpoint(page: int, size: int, query: str) -> None: ...

    benchmark(
        default_key_builder,
        endpoint,
        "app:ns",
        args=(),
        kwargs={"page": 1, "size": 50, "query": "oil"},
    )


def test_sign_and_verify(benchmark: BenchmarkFixture) -> None:
    signer = Signer("k" * 32)
    payload = JsonCoder.encode(ITEMS)

    def round_trip() -> bytes | None:
        return signer.unsign("key", signer.sign("key", payload))

    assert benchmark(round_trip) == payload


def test_encrypt_and_decrypt(benchmark: BenchmarkFixture) -> None:
    encrypter = Encrypter("k" * 32)
    payload = JsonCoder.encode(ITEMS)

    def round_trip() -> bytes | None:
        return encrypter.open("key", encrypter.seal("key", payload))

    assert benchmark(round_trip) == payload


def test_rate_limiter_hit(benchmark: BenchmarkFixture, loop: asyncio.AbstractEventLoop) -> None:
    """A request within the limit, with the in-memory backend: the limiter's own work."""
    limiter = FastAPIRateLimiter(times=10**9, minutes=1, cache=Cache(InMemoryBackend()))
    request = Request({"type": "http", "path": "/search", "client": ("192.0.2.1", 1234)})
    assert benchmark(lambda: loop.run_until_complete(limiter.hit(request))) is None
