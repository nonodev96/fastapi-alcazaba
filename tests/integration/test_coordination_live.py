"""The concurrency limiter, leader election, maintenance mode and feature flags against real
servers (see test_backends_live for the environment variables). Each test has a random prefix and
removes what it stores."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable

import pytest

from fastapi_alcazaba import Cache
from fastapi_alcazaba.concurrency import ConcurrencyLimitExceeded, FastAPIConcurrencyLimiter
from fastapi_alcazaba.flags import FastAPIFeatureFlags
from fastapi_alcazaba.leader import FastAPILeader
from fastapi_alcazaba.maintenance import FastAPIMaintenance
from fastapi_alcazaba.types import Backend
from tests.integration.test_backends_live import ENV, KINDS, _make

pytestmark = pytest.mark.integration


@pytest.fixture(name="backend", params=KINDS)
async def fixture_backend(request: pytest.FixtureRequest) -> AsyncIterator[Backend]:
    url = os.environ.get(ENV[request.param])
    if not url:
        pytest.skip(f"{ENV[request.param]} not set")
    made, close = await _make(request.param, url)
    yield made
    await close()


@pytest.fixture(name="cache")
def fixture_cache(backend: Backend) -> Cache:
    return Cache(backend, prefix=f"fastapi-alcazaba-test-{uuid.uuid4().hex}")


def needs_locks(cache: Cache) -> None:
    if not cache.backend.supports_locks:
        pytest.skip("no locks")


async def eventually(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


async def test_concurrency_limit(cache: Cache) -> None:
    """Of many processes' requests at the same time, exactly `limit` get a slot."""
    needs_locks(cache)
    processes = [FastAPIConcurrencyLimiter(3, name="ocr", cache=cache) for _ in range(5)]
    release = asyncio.Event()

    async def hold(limiter: FastAPIConcurrencyLimiter) -> bool:
        try:
            async with limiter.slot():
                await release.wait()
                return True
        except ConcurrencyLimitExceeded:
            return False

    attempts = [asyncio.create_task(hold(limiter)) for limiter in processes for _ in range(2)]
    await asyncio.sleep(1)
    release.set()
    assert sum(await asyncio.gather(*attempts)) == 3
    async with processes[0].slot():  # all released
        pass


async def test_leader_election(cache: Cache) -> None:
    needs_locks(cache)
    leaders = [FastAPILeader("outbox", ttl=1, cache=cache) for _ in range(3)]
    for leader in leaders:
        await leader.start()
    try:
        await eventually(lambda: any(leader.is_leader for leader in leaders))
        await asyncio.sleep(0.5)
        [first] = [leader for leader in leaders if leader.is_leader]
        await first.stop()
        rest = [leader for leader in leaders if leader is not first]
        await eventually(lambda: any(leader.is_leader for leader in rest))
        assert sum(leader.is_leader for leader in rest) == 1
    finally:
        for leader in leaders:
            await leader.stop()


async def test_maintenance(cache: Cache) -> None:
    here, elsewhere = FastAPIMaintenance(cache), FastAPIMaintenance(cache, refresh=0.1)
    await here.enable("Back soon", retry_after=60)
    try:
        await asyncio.sleep(0.15)
        state = await elsewhere.current()
        assert (state.enabled, state.message, state.retry_after) == (True, "Back soon", 60)
    finally:
        await here.disable()
    assert not (await elsewhere.status()).enabled


async def test_feature_flags(cache: Cache) -> None:
    here, elsewhere = FastAPIFeatureFlags(cache), FastAPIFeatureFlags(cache, refresh=0.1)
    await here.set("beta", enabled=True, percentage=0, allow=["alice"])
    try:
        assert await elsewhere.enabled("beta", "alice")
        assert not await elsewhere.enabled("beta", "bob")
    finally:
        await here.delete("beta")
    assert await elsewhere.get("beta") is None
