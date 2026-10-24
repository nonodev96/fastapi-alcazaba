import time
from typing import Any

import fakeredis
import pytest

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends._redis_common import RedisLikeBackend, escape_glob
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.memcached import MemcachedBackend, memcached_key
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.backends.valkey import ValkeyBackend


class TestInMemoryBackend:
    async def test_set_get_with_ttl(self) -> None:
        backend = InMemoryBackend()
        await backend.set("k", b"v", expire=60)
        ttl, value = await backend.get_with_ttl("k")
        assert value == b"v"
        assert 59 <= ttl <= 60

    async def test_entries_without_expire_do_not_expire(self) -> None:
        """fastapi-cache2 stored expire=None entries as already expiring."""
        backend = InMemoryBackend()
        await backend.set("k", b"v")
        assert await backend.get_with_ttl("k") == (-1, b"v")

    async def test_expired_entries_are_gone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = InMemoryBackend()
        await backend.set("k", b"v", expire=1)
        now = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: now + 2)
        assert await backend.get_with_ttl("k") == (0, None)
        assert await backend.get("k") is None

    async def test_instances_do_not_share_their_store(self) -> None:
        first, second = InMemoryBackend(), InMemoryBackend()
        await first.set("k", b"v")
        assert await second.get("k") is None

    async def test_max_size_evicts_the_least_recently_used(self) -> None:
        backend = InMemoryBackend(max_size=2)
        await backend.set("a", b"1")
        await backend.set("b", b"2")
        await backend.get("a")  # "b" is now the least recently used
        await backend.set("c", b"3")
        assert await backend.get("b") is None
        assert await backend.get("a") == b"1"
        assert await backend.get("c") == b"3"

    async def test_varying_keys_cannot_grow_it_without_limit(self) -> None:
        backend = InMemoryBackend(max_size=100)
        for i in range(1_000):
            await backend.set(f"ns:{i}", b"x")
        assert len(backend._store) == 100

    def test_max_size_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_size"):
            InMemoryBackend(max_size=0)

    async def test_clear_namespace_stops_at_the_separator(self) -> None:
        backend = InMemoryBackend()
        for key in ("app:ns:1", "app:ns:2", "app:ns10:1", "app:other:1"):
            await backend.set(key, b"x")
        assert await backend.clear(namespace="app:ns") == 2
        assert await backend.get("app:ns10:1") == b"x"
        assert await backend.get("app:other:1") == b"x"

    async def test_clear_missing_key_is_not_an_error(self) -> None:
        backend = InMemoryBackend()
        assert await backend.clear(key="missing") == 0
        await backend.set("k", b"v")
        assert await backend.clear(key="k") == 1


@pytest.fixture
def fake_redis() -> Any:
    return fakeredis.FakeAsyncRedis()


class TestRedisBackend:
    async def test_set_get_with_ttl(self, fake_redis: Any) -> None:
        backend = RedisBackend(fake_redis)
        await backend.set("k", b"v", expire=60)
        ttl, value = await backend.get_with_ttl("k")
        assert value == b"v"
        assert 59 <= ttl <= 60
        assert await backend.get("k") == b"v"

    async def test_missing_and_persistent_keys(self, fake_redis: Any) -> None:
        backend = RedisBackend(fake_redis)
        assert await backend.get_with_ttl("missing") == (0, None)
        await backend.set("k", b"v")
        assert await backend.get_with_ttl("k") == (-1, b"v")

    async def test_clear_namespace(self, fake_redis: Any) -> None:
        backend = RedisBackend(fake_redis, scan_count=2)  # several SCAN pages and UNLINK batches
        for i in range(5):
            await fake_redis.set(f"app:ns:{i}", b"x")
        await fake_redis.set("app:ns10:1", b"x")
        await fake_redis.set("other", b"x")
        assert await backend.clear(namespace="app:ns") == 5
        assert sorted(await fake_redis.keys()) == [b"app:ns10:1", b"other"]

    async def test_clear_namespace_is_not_a_script_injection(self, fake_redis: Any) -> None:
        """fastapi-cache2 pasted the namespace into a Lua script: this one ran FLUSHALL."""
        await fake_redis.set("keep", b"x")
        backend = RedisBackend(fake_redis)
        assert await backend.clear(namespace="x') redis.call('FLUSHALL') --") == 0
        assert await fake_redis.get("keep") == b"x"

    async def test_clear_namespace_escapes_glob_patterns(self, fake_redis: Any) -> None:
        for key in ("app:a*:1", "app:ab:1", "app:a?:1", "app:ac:1", "app:[ab]:1", "app:a:1"):
            await fake_redis.set(key, b"x")
        backend = RedisBackend(fake_redis)
        assert await backend.clear(namespace="app:a*") == 1
        assert await backend.clear(namespace="app:a?") == 1
        assert await backend.clear(namespace="app:[ab]") == 1
        assert sorted(await fake_redis.keys()) == [b"app:a:1", b"app:ab:1", b"app:ac:1"]

    async def test_clear_key(self, fake_redis: Any) -> None:
        backend = RedisBackend(fake_redis)
        await fake_redis.set("k", b"x")
        assert await backend.clear(key="k") == 1
        assert await backend.clear(key="k") == 0
        assert await backend.clear() == 0

    async def test_cluster_flag(self, fake_redis: Any) -> None:
        backend = RedisLikeBackend(fake_redis, is_cluster=True, scan_count=2)
        for i in range(3):
            await fake_redis.set(f"ns:{i}", b"x")
        assert await backend.clear(namespace="ns") == 3
        assert await fake_redis.keys() == []

    def test_scan_count_must_be_positive(self, fake_redis: Any) -> None:
        with pytest.raises(ValueError, match="scan_count"):
            RedisBackend(fake_redis, scan_count=0)


def test_escape_glob() -> None:
    assert escape_glob(r"a*b?c[d]e\f") == r"a\*b\?c\[d\]e\\f"


async def test_valkey_backend(fake_redis: Any) -> None:
    """valkey-py has redis-py's API; fakeredis stands in for the server."""
    backend = ValkeyBackend(fake_redis)
    assert backend.valkey is fake_redis
    assert not backend.is_cluster
    await backend.set("ns:k", b"v", expire=60)
    assert (await backend.get_with_ttl("ns:k"))[1] == b"v"
    assert await backend.clear(namespace="ns") == 1


class FakeMemcache:
    def __init__(self) -> None:
        self.data: dict[bytes, bytes] = {}
        self.exptimes: dict[bytes, int] = {}

    async def get(self, key: bytes) -> bytes | None:
        return self.data.get(key)

    async def set(self, key: bytes, value: bytes, exptime: int = 0) -> bool:
        self.data[key] = value
        self.exptimes[key] = exptime
        return True

    async def delete(self, key: bytes) -> bool:
        return self.data.pop(key, None) is not None


class TestMemcachedBackend:
    async def test_set_get_and_unknown_ttl(self) -> None:
        backend = MemcachedBackend(FakeMemcache())  # type: ignore[arg-type]
        await backend.set("k", b"v", expire=60)
        assert await backend.get_with_ttl("k") == (-1, b"v")
        assert await backend.get_with_ttl("missing") == (0, None)

    async def test_long_expire_becomes_a_timestamp(self) -> None:
        mcache = FakeMemcache()
        backend = MemcachedBackend(mcache)  # type: ignore[arg-type]
        await backend.set("short", b"v", expire=60)
        await backend.set("long", b"v", expire=40 * 24 * 3600)
        assert mcache.exptimes[b"short"] == 60
        assert mcache.exptimes[b"long"] > time.time()

    def test_invalid_keys_are_hashed(self) -> None:
        assert memcached_key("app:ns:abc") == b"app:ns:abc"
        for bad in ("with space", "x" * 300, "ñ", "tab\t"):
            hashed = memcached_key(bad)
            assert hashed.startswith(b"fastapi_alcazaba:sha256:")
            assert len(hashed) <= 250

    async def test_clear(self) -> None:
        backend = MemcachedBackend(FakeMemcache())  # type: ignore[arg-type]
        await backend.set("k", b"v")
        assert await backend.clear(key="k") == 1
        assert await backend.clear(key="k") == 0
        with pytest.raises(NotImplementedError):
            await backend.clear(namespace="ns")


class TestFastAPICacheClear:
    async def test_clear_key_with_a_prefix_only_deletes_that_key(self) -> None:
        """Upstream PR #529."""
        backend = InMemoryBackend()
        FastAPICache.init(backend, prefix="app")
        await backend.set("app:ns:1", b"x")
        await backend.set("app:ns:2", b"x")
        assert await FastAPICache.clear(key="app:ns:1") == 1
        assert await backend.get("app:ns:2") == b"x"

    async def test_clear_namespace(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, prefix="app")
        await backend.set("app:ns:1", b"x")
        await backend.set("app:other:1", b"x")
        assert await FastAPICache.clear(namespace="ns") == 1
        assert await FastAPICache.clear() == 1


class FakeDynamoClient:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    async def get_item(self, TableName: str, Key: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        item = self.items.get(Key["key"]["S"])
        return {} if item is None else {"Item": item}

    async def put_item(self, TableName: str, Item: dict[str, Any]) -> None:  # noqa: N803
        self.items[Item["key"]["S"]] = Item

    async def delete_item(self, TableName: str, Key: dict[str, Any], ReturnValues: str) -> Any:  # noqa: N803
        item = self.items.pop(Key["key"]["S"], None)
        return {"Attributes": item} if item else {}

    async def __aexit__(self, *args: Any) -> None:
        pass


class TestDynamoBackend:
    @pytest.fixture
    def backend(self) -> Any:
        from fastapi_alcazaba.backends.dynamodb import DynamoBackend

        backend = DynamoBackend(table_name="cache", region="eu-west-1")
        backend.client = FakeDynamoClient()  # type: ignore[assignment]
        return backend

    async def test_round_trip(self, backend: Any) -> None:
        await backend.set("k", b"v", expire=60)
        ttl, value = await backend.get_with_ttl("k")
        assert value == b"v"
        assert 59 <= ttl <= 60
        await backend.set("forever", b"v")
        assert await backend.get_with_ttl("forever") == (-1, b"v")
        assert await backend.get("missing") is None

    async def test_expired_items_still_in_the_table_are_ignored(self, backend: Any) -> None:
        await backend.set("k", b"v", expire=60)
        backend.client.items["k"]["ttl"]["N"] = str(int(time.time()) - 1)
        assert await backend.get_with_ttl("k") == (0, None)

    async def test_clear(self, backend: Any) -> None:
        await backend.set("k", b"v")
        assert await backend.clear(key="k") == 1
        assert await backend.clear(key="k") == 0
        assert await backend.clear() == 0
        with pytest.raises(NotImplementedError):
            await backend.clear(namespace="ns")

    async def test_needs_init(self) -> None:
        from fastapi_alcazaba.backends.dynamodb import DynamoBackend

        backend = DynamoBackend(table_name="cache")
        with pytest.raises(RuntimeError, match="init"):
            await backend.get("k")
        await backend.close()

    async def test_close(self, backend: Any) -> None:
        await backend.close()
        assert backend.client is None
