"""Against real servers, each enabled by its environment variable:

    REDIS_URL=redis://localhost:6379/15            VALKEY_URL=valkey://localhost:6380/15
    CLUSTER_URL=redis://localhost:16382            (a Valkey or Redis cluster)
    MEMCACHED_URL=localhost:11211                  DYNAMODB_URL=http://localhost:8000

Every key carries a random prefix and only those keys are deleted, so it's safe on a shared
database (DynamoDB gets a table of its own, dropped afterwards).
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import pytest

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.compression import Codec
from fastapi_alcazaba.decorator import cache
from fastapi_alcazaba.key_builder import default_key_builder
from fastapi_alcazaba.types import Backend

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


@pytest.fixture(params=KINDS)
async def backend(request: pytest.FixtureRequest) -> AsyncIterator[Backend]:
    url = os.environ.get(ENV[request.param])
    if not url:
        pytest.skip(f"{ENV[request.param]} not set")
    made, close = await _make(request.param, url)
    yield made
    await close()


@pytest.fixture
def prefix() -> str:
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
    assert await backend.invalidate_tags([other]) == 1


@pytest.mark.parametrize(
    ("protection", "compression"), [("signing_key", None), ("encryption_key", "zstd")]
)
async def test_through_the_decorator(
    backend: Backend, prefix: str, protection: str, compression: Codec | None
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
