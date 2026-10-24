"""TieredBackend over real Redis and Valkey: invalidations travel on real pub/sub between two
clients, as between two processes."""

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.backends.tiered import TieredBackend
from fastapi_alcazaba.backends.valkey import ValkeyBackend


async def eventually(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@pytest.fixture(params=["redis", "valkey"])
async def processes(request: pytest.FixtureRequest) -> AsyncIterator[tuple[TieredBackend, ...]]:
    url = os.environ.get("REDIS_URL" if request.param == "redis" else "VALKEY_URL")
    if not url:
        pytest.skip("no server for this backend")
    channel = f"fastapi-alcazaba-test-{uuid.uuid4().hex}"
    clients: list[Any] = []
    tiers: list[TieredBackend] = []
    for _ in range(2):
        if request.param == "redis":
            import redis.asyncio

            client: Any = redis.asyncio.from_url(url)
            backend: Any = RedisBackend(client)
        else:
            import valkey.asyncio

            client = valkey.asyncio.from_url(url)  # type: ignore[no-untyped-call]
            backend = ValkeyBackend(client)
        clients.append(client)
        tiers.append(TieredBackend(backend, local_ttl=60, invalidation_channel=channel))
    for tier in tiers:
        await tier.get("warm-up")
    await eventually(lambda: all(tier._subscribed for tier in tiers))
    yield tuple(tiers)
    for tier in tiers:
        await tier.close()
    for client in clients:
        await client.aclose()


async def test_writes_reach_the_other_process(processes: tuple[TieredBackend, ...]) -> None:
    first, second = processes
    key = f"fastapi-alcazaba-test-{uuid.uuid4().hex}:k"
    await first.set(key, b"old", expire=60)
    assert await second.get(key) == b"old"
    assert key in second.local._store
    await first.set(key, b"new", expire=60)
    await eventually(lambda: key not in second.local._store)
    assert await second.get(key) == b"new"
    await first.clear(key=key)


async def test_tag_invalidations_reach_the_other_process(
    processes: tuple[TieredBackend, ...],
) -> None:
    first, second = processes
    prefix = f"fastapi-alcazaba-test-{uuid.uuid4().hex}"
    key, tag = f"{prefix}:product:1", f"{prefix}:tag:{{p1}}"
    await first.set(key, b"v", expire=60)
    await first.add_tags(key, [tag], 60)
    assert await second.get(key) == b"v"
    await second.local.add_tags(key, [tag], 60)
    assert await first.invalidate_tags([tag]) == 1
    await eventually(lambda: key not in second.local._store)
    assert await second.get(key) is None
