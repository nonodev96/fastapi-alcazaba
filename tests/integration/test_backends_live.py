"""Against real servers, each enabled by its environment variable:

    REDIS_URL=redis://localhost:6379/15            VALKEY_URL=valkey://localhost:6380/15
    CLUSTER_URL=redis://localhost:16382            (a Valkey or Redis cluster)
    MEMCACHED_URL=localhost:11211                  DYNAMODB_URL=http://localhost:8000

Every key carries a random prefix and only those keys are deleted, so it's safe on a shared
database (DynamoDB gets a table of its own, dropped afterwards).
"""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from fastapi_alcazaba import Cache, FastAPICache, FastAPIScheduler
from fastapi_alcazaba.auth import OneTimeCodes, TokenBlocklist
from fastapi_alcazaba.backends import Backend
from fastapi_alcazaba.idempotency import IdempotencyMiddleware
from fastapi_alcazaba.rate_limit import FastAPIRateLimiter
from fastapi_alcazaba.response_cache import cache, default_key_builder
from fastapi_alcazaba.response_cache.compression import Codec

pytestmark = pytest.mark.integration

KINDS = [
    "redis",
    "valkey",
    "glide",
    "redis-cluster",
    "valkey-cluster",
    "glide-cluster",
    "memcached",
    "dynamodb",
    "tiered-redis",
    "tiered-valkey",
]
ENV = {
    "redis": "REDIS_URL",
    "valkey": "VALKEY_URL",
    "glide": "VALKEY_URL",
    "redis-cluster": "CLUSTER_URL",
    "valkey-cluster": "CLUSTER_URL",
    "glide-cluster": "CLUSTER_URL",
    "memcached": "MEMCACHED_URL",
    "dynamodb": "DYNAMODB_URL",
    "tiered-redis": "REDIS_URL",
    "tiered-valkey": "VALKEY_URL",
}


def _host_port(url: str, default_port: int) -> tuple[str, int, int]:
    parsed = urlparse(url if "://" in url else f"//{url}")
    database = int(parsed.path.strip("/") or 0)
    return parsed.hostname or "localhost", parsed.port or default_port, database


async def _dynamodb(url: str) -> tuple[Backend, Any]:
    from fastapi_alcazaba.backends.dynamodb import DynamoBackend

    for name, value in (("AWS_ACCESS_KEY_ID", "test"), ("AWS_SECRET_ACCESS_KEY", "test")):
        os.environ.setdefault(name, value)
    table = f"fastapi-alcazaba-test-{uuid.uuid4().hex[:8]}"
    backend = DynamoBackend(table, region="us-east-1", endpoint_url=url)
    await backend.init()
    client = backend._get_client()
    await client.create_table(
        TableName=table,
        KeySchema=[{"AttributeName": "key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    async def close() -> None:
        await client.delete_table(TableName=table)
        await backend.close()

    return backend, close


async def _make(kind: str, url: str) -> tuple[Backend, Any]:
    if kind.startswith("tiered-"):
        from fastapi_alcazaba.backends.tiered import TieredBackend

        remote, close_remote = await _make(kind.removeprefix("tiered-"), url)
        tiered = TieredBackend(remote)

        async def close() -> None:
            await tiered.close()
            await close_remote()

        return tiered, close
    if kind == "redis":
        import redis.asyncio

        from fastapi_alcazaba.backends.redis import RedisBackend

        client: Any = redis.asyncio.from_url(url)
        return RedisBackend(client, scan_count=3), client.aclose
    if kind == "valkey":
        import valkey.asyncio

        from fastapi_alcazaba.backends.valkey import ValkeyBackend

        client = valkey.asyncio.from_url(url)  # type: ignore[no-untyped-call]
        return ValkeyBackend(client, scan_count=3), client.aclose
    if kind == "redis-cluster":
        from redis.asyncio.cluster import RedisCluster

        from fastapi_alcazaba.backends.redis import RedisBackend

        client = RedisCluster.from_url(url)
        return RedisBackend(client, scan_count=3), client.aclose
    if kind == "valkey-cluster":
        from valkey.asyncio.cluster import ValkeyCluster

        from fastapi_alcazaba.backends.valkey import ValkeyBackend

        client = ValkeyCluster.from_url(url)
        return ValkeyBackend(client, scan_count=3), client.aclose
    if kind in ("glide", "glide-cluster"):
        from glide import (
            GlideClient,
            GlideClientConfiguration,
            GlideClusterClient,
            GlideClusterClientConfiguration,
            NodeAddress,
        )

        from fastapi_alcazaba.backends.glide import GlideBackend

        host, port, database = _host_port(url, 6379)
        if kind == "glide":
            client = await GlideClient.create(
                GlideClientConfiguration([NodeAddress(host, port)], database_id=database)
            )
        else:
            client = await GlideClusterClient.create(
                GlideClusterClientConfiguration([NodeAddress(host, port)])
            )
        return GlideBackend(client, scan_count=3), client.close
    if kind == "memcached":
        import aiomcache

        from fastapi_alcazaba.backends.memcached import MemcachedBackend

        host, port, _ = _host_port(url, 11211)
        client = aiomcache.Client(host, port)
        return MemcachedBackend(client), client.close
    return await _dynamodb(url)


@pytest.fixture(name="backend", params=KINDS)
async def fixture_backend(request: pytest.FixtureRequest) -> AsyncIterator[Backend]:
    url = os.environ.get(ENV[request.param])
    if not url:
        pytest.skip(f"{ENV[request.param]} not set")
    made, close = await _make(request.param, url)
    yield made
    await close()


@pytest.fixture(name="prefix")
def fixture_prefix() -> str:
    return f"fastapi-alcazaba-test-{uuid.uuid4().hex}"


async def test_round_trip(backend: Backend, prefix: str) -> None:
    await backend.set(f"{prefix}:k", b"v", expire=60)
    ttl, value = await backend.get_with_ttl(f"{prefix}:k")
    assert value == b"v"
    assert ttl == -1 or 0 < ttl <= 60  # memcached can't tell
    assert await backend.get(f"{prefix}:k") == b"v"
    assert await backend.get_with_ttl(f"{prefix}:missing") == (0, None)
    assert await backend.clear(key=f"{prefix}:k") == 1
    assert await backend.get(f"{prefix}:k") is None


async def test_clear_namespace(backend: Backend, prefix: str) -> None:
    if isinstance(backend, _no_namespace_clear()):
        pytest.skip("this backend can't delete by namespace")
    for i in range(7):
        await backend.set(f"{prefix}:ns:{i}", b"v", expire=60)
    await backend.set(f"{prefix}:ns10:1", b"v", expire=60)
    assert await backend.clear(namespace=f"{prefix}:ns") == 7
    assert await backend.get(f"{prefix}:ns10:1") == b"v"
    assert await backend.clear(namespace="x') redis.call('FLUSHALL') --") == 0
    assert await backend.get(f"{prefix}:ns10:1") == b"v"
    assert await backend.clear(namespace=prefix) == 1


async def test_locks(backend: Backend, prefix: str) -> None:
    if not backend.supports_locks:
        pytest.skip("no locks")
    lock = f"{prefix}:lock"
    assert await backend.acquire_lock(lock, "mine", 5)
    assert not await backend.acquire_lock(lock, "theirs", 5)
    await backend.release_lock(lock, "theirs")
    assert not await backend.acquire_lock(lock, "theirs", 5)
    await backend.release_lock(lock, "mine")
    assert await backend.acquire_lock(lock, "theirs", 0.05)
    await asyncio.sleep(0.2)  # expired
    assert await backend.acquire_lock(lock, "mine", 5)
    await backend.release_lock(lock, "mine")


async def test_extend_lock(backend: Backend, prefix: str) -> None:
    if not backend.supports_locks:
        pytest.skip("no locks")
    lock = f"{prefix}:lock:{{x}}"
    assert await backend.acquire_lock(lock, "mine", 0.5)
    assert await backend.extend_lock(lock, "mine", 5)
    assert not await backend.extend_lock(lock, "theirs", 5)
    await asyncio.sleep(0.8)
    assert not await backend.acquire_lock(lock, "theirs", 5)  # extended
    await backend.release_lock(lock, "mine")
    assert await backend.acquire_lock(lock, "theirs", 0.05)
    await asyncio.sleep(0.2)
    assert not await backend.extend_lock(lock, "theirs", 5)  # expired


async def test_cache_lock(backend: Backend, prefix: str) -> None:
    """Renewed while held: it outlives its ttl, and only one of many contenders gets it."""
    if not backend.supports_locks:
        pytest.skip("no locks")
    app_cache = Cache(backend, prefix=prefix)
    async with app_cache.lock("job", ttl=1) as lock:
        await asyncio.sleep(1.5)
        assert lock.held
        assert not await app_cache.lock("job").acquire()
    contenders = [app_cache.lock("race") for _ in range(10)]
    assert sum(await asyncio.gather(*(c.acquire() for c in contenders))) == 1
    for contender in contenders:
        await contender.release()


async def test_concurrent_deletes_of_one_entry(backend: Backend, prefix: str) -> None:
    """Exactly one of them is told the entry was there: what makes a one-time code work once."""
    key = f"{prefix}:once"
    for _ in range(300):  # a race: it doesn't show every time
        await backend.set(key, b"v")
        assert sum(await asyncio.gather(*(backend.clear(key=key) for _ in range(5)))) == 1


async def test_one_time_codes(backend: Backend, prefix: str) -> None:
    """Of several requests with the right code at the same time, exactly one succeeds."""
    codes = OneTimeCodes("s" * 32, Cache(backend, prefix=prefix), max_attempts=10)
    code = await codes.issue("someone@example.com", purpose="login")
    tries = [codes.verify("someone@example.com", code, purpose="login") for _ in range(5)]
    assert sum(await asyncio.gather(*tries)) == 1
    assert not await codes.verify("someone@example.com", code, purpose="login")


async def test_scheduled_ticks_run_once(backend: Backend, prefix: str) -> None:
    """Two processes' schedulers: the backend gives each tick to one of them."""
    ticks: list[datetime | None] = []
    schedulers = [FastAPIScheduler(Cache(backend, prefix=prefix)) for _ in range(2)]
    for scheduler in schedulers:

        @scheduler.every(seconds=0.25, name="job")
        async def job(scheduler: FastAPIScheduler = scheduler) -> None:
            ticks.append(scheduler.next_run("job"))  # the tick being run

    for scheduler in schedulers:
        await scheduler.start()
    await asyncio.sleep(1.4)
    for scheduler in schedulers:
        await scheduler.stop()
    assert len(ticks) >= 4
    assert len(ticks) == len(set(ticks))


async def test_token_blocklist(backend: Backend, prefix: str) -> None:
    blocklist = TokenBlocklist(Cache(backend, prefix=prefix, signing_key="k" * 32))
    await blocklist.revoke("jti-1", ttl=60)
    await blocklist.revoke_subject("user", ttl=60)
    assert await blocklist.is_revoked("jti-1")
    assert not await blocklist.is_revoked("jti-2")
    assert await blocklist.is_revoked("jti-2", subject="user", issued_at=0)


async def test_idempotency(backend: Backend, prefix: str) -> None:
    """Five copies of a request at once: it runs once, the others wait for it or get it back."""
    if not backend.supports_locks:
        pytest.skip("no locks")
    app = FastAPI()
    Cache(backend, prefix=prefix, encryption_key="e" * 32).install(app)
    app.add_middleware(IdempotencyMiddleware)
    runs: list[int] = []

    @app.post("/orders", status_code=201)
    async def create() -> dict[str, int]:
        runs.append(1)
        await asyncio.sleep(0.3)
        return {"id": len(runs)}

    key = {"Idempotency-Key": f'"{uuid.uuid4()}"'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        responses = await asyncio.gather(*(c.post("/orders", headers=key) for _ in range(5)))
        assert sorted(r.status_code for r in responses) == [201, 409, 409, 409, 409]
        replay = await c.post("/orders", headers=key)
    assert (replay.headers["idempotent-replayed"], replay.json()) == ("true", {"id": 1})
    assert runs == [1]


async def test_tags(backend: Backend, prefix: str) -> None:
    if not backend.supports_tags:
        pytest.skip("no tags")
    tag, other = f"{prefix}:tag:{{a}}", f"{prefix}:tag:{{b}}"
    for i in range(3):
        await backend.set(f"{prefix}:e{i}", b"v", expire=60)
        await backend.add_tags(f"{prefix}:e{i}", [tag] if i < 2 else [other], 60)
    assert await backend.invalidate_tags([tag]) == 2
    assert await backend.get(f"{prefix}:e0") is None
    assert await backend.get(f"{prefix}:e2") == b"v"
    assert await backend.invalidate_tags([tag]) == 0
    assert await backend.invalidate_tags_with_keys([other]) == (1, [f"{prefix}:e2"])


async def test_counters(backend: Backend, prefix: str) -> None:
    assert backend.supports_counters
    key = f"{prefix}:counter"
    assert await backend.get_counter(key) == 0
    assert await backend.incr(key, expire=60) == 1
    assert await backend.incr(key, 4, expire=60) == 5
    assert await backend.incr(key, -2, expire=60) == 3
    assert await backend.get_counter(key) == 3
    # Atomic: 50 concurrent increments, each sees its own value.
    race = [backend.incr(f"{prefix}:race", expire=60) for _ in range(50)]
    assert sorted(await asyncio.gather(*race)) == list(range(1, 51))
    await backend.incr(f"{prefix}:short", expire=1)
    await asyncio.sleep(2.5)  # memcached rounds expiry times to the second
    assert await backend.get_counter(f"{prefix}:short") == 0


async def test_incr_window(backend: Backend, prefix: str) -> None:
    """50 concurrent requests for 10 places: exactly 10 counted. Both windows share a hash tag,
    as the limiter's keys do: a Lua script with keys in two cluster slots would fail."""
    base = f"{prefix}:window:{{client}}"
    await backend.incr(f"{base}:0", 4, expire=60)  # weighs 2
    calls = [
        backend.incr_window(f"{base}:1", f"{base}:0", amount=1, limit=12, weight=0.5, expire=60)
        for _ in range(50)
    ]
    results = await asyncio.gather(*calls)
    assert sum(added for added, _, _ in results) == 10
    assert await backend.get_counter(f"{base}:1") == 10


async def test_rate_limiter(backend: Backend, prefix: str) -> None:
    """Concurrent requests over the limit: exactly `times` get through."""
    limiter = FastAPIRateLimiter(times=3, minutes=1, cache=Cache(backend, prefix=prefix))
    request = Request({"type": "http", "path": "/search", "client": ("192.0.2.1", 1234)})
    results = await asyncio.gather(*(limiter.hit(request) for _ in range(10)))
    assert sum(result is None for result in results) == 3
    other = Request({"type": "http", "path": "/search", "client": ("192.0.2.2", 1234)})
    assert await limiter.hit(other) is None


@pytest.mark.parametrize(
    ("protection", "compression"), [("signing_key", None), ("encryption_key", "zstd")]
)
async def test_through_the_decorator(
    backend: Backend,
    prefix: str,
    protection: str,
    compression: Codec | None,
) -> None:
    key = "s" * 32
    FastAPICache.init(
        backend,
        prefix=prefix,
        distributed_lock=backend.supports_locks,
        signing_key=key if protection == "signing_key" else None,
        encryption_key=key if protection == "encryption_key" else None,
        compression=compression,
        compression_min_size=0,
    )
    calls: list[int] = []

    async def value(x: int) -> dict[str, str]:
        calls.append(x)
        await asyncio.sleep(0.05)
        return {"secret": f"confidential-{x}"}

    cached = cache(expire=60, namespace="ns", vary_headers=())(value)
    expected = {"secret": "confidential-1"}
    assert list(await asyncio.gather(cached(1), cached(1))) == [expected, expected]
    assert await cached(1) == expected
    assert calls == [1]
    stored = await backend.get(default_key_builder(value, f"{prefix}:ns", args=(1,), kwargs={}))
    assert stored is not None
    assert (b"confidential-1" in stored) is (protection == "signing_key")
    if not isinstance(backend, _no_namespace_clear()):
        assert await FastAPICache.clear(namespace="ns") == 1


def _no_namespace_clear() -> tuple[type, ...]:
    kinds: list[type] = []
    try:
        from fastapi_alcazaba.backends.memcached import MemcachedBackend

        kinds.append(MemcachedBackend)
    except ImportError:  # pragma: no cover
        pass
    try:
        from fastapi_alcazaba.backends.dynamodb import DynamoBackend

        kinds.append(DynamoBackend)
    except ImportError:  # pragma: no cover
        pass
    return tuple(kinds)
