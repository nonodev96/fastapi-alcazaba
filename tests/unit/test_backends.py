import time
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

import fakeredis
import pytest
from aiomcache.exceptions import ClientException
from botocore.exceptions import ClientError

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends._redis_common import RedisLikeBackend, escape_glob
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.memcached import MemcachedBackend, memcached_key
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.backends.valkey import ValkeyBackend
from fastapi_alcazaba.types import Backend, is_state_key


async def check_incr_window(backend: Backend) -> None:
    """incr_window adds only what fits: previous × weight + current + amount ≤ limit."""

    async def window(key: str, amount: int, weight: float) -> tuple[bool, int, int]:
        return await backend.incr_window(
            f"{key}:1", f"{key}:0", amount=amount, limit=5, weight=weight, expire=60
        )

    await backend.incr("w:0", 4, expire=60)
    # The previous window weighs 4 × 0.5 = 2: 3 fit.
    assert await window("w", 2, 0.5) == (True, 2, 4)
    assert await window("w", 2, 0.5) == (False, 2, 4)  # and it isn't counted
    assert await window("w", 1, 0.5) == (True, 3, 4)
    assert await window("w", 1, 0.25) == (True, 4, 4)  # 4 × 0.25 = 1 now
    assert await backend.get_counter("w:1") == 4
    assert await window("w", 2, 1.0) == (False, 4, 4)  # the previous window weighs all of it
    assert await window("new", 5, 1.0) == (True, 5, 0)
    assert await window("new", 1, 1.0) == (False, 5, 0)


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

    async def test_counters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = InMemoryBackend()
        assert backend.supports_counters
        assert await backend.get_counter("c") == 0
        assert await backend.incr("c", expire=10) == 1
        assert await backend.incr("c", 4, expire=10) == 5
        assert await backend.incr("c", -1, expire=10) == 4
        assert await backend.get_counter("c") == 4
        now = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: now + 11)  # created 11 s ago
        assert await backend.get_counter("c") == 0
        assert await backend.incr("c", expire=10) == 1

    async def test_incr_window(self) -> None:
        await check_incr_window(InMemoryBackend())

    async def test_counters_without_expiry_and_bounded(self) -> None:
        backend = InMemoryBackend(max_size=2)
        for key in ("a", "b", "c"):
            await backend.incr(key)
        assert await backend.get_with_ttl("c") == (-1, b"1")
        assert await backend.get_counter("a") == 0  # evicted, like any entry


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

    async def test_counters(self, fake_redis: Any) -> None:
        backend = RedisBackend(fake_redis)
        assert backend.supports_counters
        assert await backend.get_counter("c") == 0
        assert await backend.incr("c", expire=60) == 1
        assert await backend.incr("c", 2, expire=60) == 3
        assert await backend.incr("c", -1, expire=60) == 2
        assert await backend.get_counter("c") == 2
        assert 0 < await fake_redis.ttl("c") <= 60

    async def test_incr_window(self, fake_redis: Any) -> None:
        """In one Lua script: one round trip, nothing between the check and the addition."""
        await check_incr_window(RedisBackend(fake_redis))
        assert 0 < await fake_redis.ttl("w:1") <= 60
        assert await fake_redis.exists("new:0") == 0  # reading a window doesn't create it

    async def test_counters_keep_their_first_expiry(self, fake_redis: Any) -> None:
        backend = RedisBackend(fake_redis)
        await backend.incr("c", expire=60)
        await fake_redis.expire("c", 5)
        await backend.incr("c", expire=60)
        assert await fake_redis.ttl("c") <= 5

    async def test_a_counter_left_without_expiry_gets_one(self, fake_redis: Any) -> None:
        backend = RedisBackend(fake_redis)
        await fake_redis.set("c", 7)
        assert await backend.incr("c", expire=60) == 8
        assert 0 < await fake_redis.ttl("c") <= 60
        await backend.incr("forever")
        assert await fake_redis.ttl("forever") == -1


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

    async def incr(self, key: bytes, increment: int = 1) -> int:
        return self._change(b"incr", key, increment)

    async def decr(self, key: bytes, decrement: int = 1) -> int:
        return self._change(b"decr", key, -decrement)

    def _change(self, command: bytes, key: bytes, delta: int) -> int:
        if key not in self.data:  # what aiomcache 0.8 does, though it documents returning None
            raise ClientException(f"Memcached {command!s} command failed", bytearray(b"NOT_FOUND"))
        if not self.data[key].isdigit():
            raise ClientException(
                f"Memcached {command!s} command failed", bytearray(b"CLIENT_ERROR")
            )
        value = max(0, int(self.data[key]) + delta)  # memcached stops at 0
        self.data[key] = str(value).encode()
        return value

    async def add(self, key: bytes, value: bytes, exptime: int = 0) -> bool:
        if key in self.data:
            return False
        return await self.set(key, value, exptime)


class RacingMemcache(FakeMemcache):
    """Another client creates the counter between our incr and our add."""

    async def add(self, key: bytes, value: bytes, exptime: int = 0) -> bool:
        if key not in self.data:
            self.data[key] = b"10"
        return await super().add(key, value, exptime)


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

    async def test_counters(self) -> None:
        mcache = FakeMemcache()
        backend = MemcachedBackend(mcache)  # type: ignore[arg-type]
        assert backend.supports_counters
        assert await backend.get_counter("c") == 0
        assert await backend.incr("c", expire=60) == 1
        assert mcache.exptimes[b"c"] == 60
        assert await backend.incr("c", 3, expire=60) == 4
        assert await backend.incr("c", -1, expire=60) == 3
        assert await backend.get_counter("c") == 3
        assert await backend.incr("new", -1, expire=60) == 0  # no negative counters

    async def test_incr_window_adds_and_takes_back(self) -> None:
        """memcached has no conditional increment: Backend's default."""
        await check_incr_window(MemcachedBackend(FakeMemcache()))  # type: ignore[arg-type]

    async def test_counter_created_by_another_client(self) -> None:
        backend = MemcachedBackend(RacingMemcache())  # type: ignore[arg-type]
        assert await backend.incr("c", expire=60) == 11

    async def test_other_errors_go_through(self) -> None:
        backend = MemcachedBackend(FakeMemcache())  # type: ignore[arg-type]
        await backend.set("not a number", b"text")
        with pytest.raises(ClientException):
            await backend.incr("not a number")


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

    async def get_item(
        self,
        TableName: str,  # noqa: N803
        Key: dict[str, Any],  # noqa: N803
        ConsistentRead: bool = False,  # noqa: N803
    ) -> dict[str, Any]:
        item = self.items.get(Key["key"]["S"])
        return {} if item is None else {"Item": item}

    async def put_item(self, TableName: str, Item: dict[str, Any], **kwargs: Any) -> None:  # noqa: N803
        key = Item["key"]["S"]
        if "ConditionExpression" in kwargs:  # only DynamoBackend.acquire_lock's
            assert kwargs["ConditionExpression"] == "attribute_not_exists(#key) OR #expires < :now"
            existing = self.items.get(key)
            now = int(kwargs["ExpressionAttributeValues"][":now"]["N"])
            if existing is not None and int(existing["lock_expires"]["N"]) >= now:
                conditional_check_failed()
        self.items[key] = Item

    async def delete_item(self, TableName: str, Key: dict[str, Any], **kwargs: Any) -> Any:  # noqa: N803
        key = Key["key"]["S"]
        if "ConditionExpression" in kwargs:  # only DynamoBackend.release_lock's
            assert kwargs["ConditionExpression"] == "#token = :token"
            existing = self.items.get(key)
            token = kwargs["ExpressionAttributeValues"][":token"]
            if existing is None or existing["token"] != token:
                conditional_check_failed()
        item = self.items.pop(key, None)
        return {"Attributes": item} if item else {}

    async def update_item(self, **kwargs: Any) -> dict[str, Any]:
        """Only what DynamoBackend sends: ADD to a number, SET if_not_exists, and the condition
        "attribute_not_exists(#count) OR #count <= :at_most"; or extend_lock's SET."""
        names, values = kwargs["ExpressionAttributeNames"], kwargs["ExpressionAttributeValues"]
        if kwargs["UpdateExpression"] == "SET #expires = :expires, #ttl = :ttl":
            assert kwargs["ConditionExpression"] == "#token = :token AND #expires >= :now"
            item = self.items.get(kwargs["Key"]["key"]["S"])
            if (
                item is None
                or item["token"] != values[":token"]
                or int(item["lock_expires"]["N"]) < int(values[":now"]["N"])
            ):
                conditional_check_failed()
            item["lock_expires"], item["ttl"] = values[":expires"], values[":ttl"]
            return {}
        assert kwargs["ReturnValues"] == "UPDATED_NEW"
        existing = self.items.get(kwargs["Key"]["key"]["S"], {}).get(names["#count"])
        if "ConditionExpression" in kwargs:
            assert kwargs["ConditionExpression"] == (
                "attribute_not_exists(#count) OR #count <= :at_most"
            )
            if existing is not None and int(existing["N"]) > int(values[":at_most"]["N"]):
                conditional_check_failed()
        item = self.items.setdefault(kwargs["Key"]["key"]["S"], dict(kwargs["Key"]))
        count = int(item.get(names["#count"], {}).get("N", "0")) + int(values[":amount"]["N"])
        item[names["#count"]] = {"N": str(count)}
        updated = {names["#count"]: item[names["#count"]]}
        if "#ttl" in names:
            item.setdefault(names["#ttl"], values[":ttl"])
            updated[names["#ttl"]] = item[names["#ttl"]]
        return {"Attributes": updated}

    async def __aexit__(self, *args: Any) -> None:
        pass


def conditional_check_failed() -> NoReturn:
    error = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}
    raise ClientError(error, "Operation")  # type: ignore[arg-type]


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

    async def test_counters(self, backend: Any) -> None:
        assert backend.supports_counters
        assert await backend.get_counter("c") == 0
        assert await backend.incr("c", expire=60) == 1
        assert await backend.incr("c", 2, expire=60) == 3
        assert await backend.incr("c", -1) == 2
        assert await backend.get_counter("c") == 2
        expires_at = int(backend.client.items["c"]["ttl"]["N"])
        assert time.time() < expires_at <= time.time() + 60
        await backend.incr("forever")
        assert "ttl" not in backend.client.items["forever"]
        assert await backend.get_counter("forever") == 1

    async def test_incr_window(self, backend: Any) -> None:
        """A conditional update: atomic for the current window."""
        await check_incr_window(backend)

    async def test_locks(self, backend: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Conditional writes: only one holder, and only the holder releases or extends it."""
        assert backend.supports_locks
        assert await backend.acquire_lock("lock", "mine", 5)
        assert not await backend.acquire_lock("lock", "theirs", 5)
        await backend.release_lock("lock", "theirs")  # not theirs to release
        assert not await backend.acquire_lock("lock", "theirs", 5)
        assert await backend.extend_lock("lock", "mine", 60)
        assert not await backend.extend_lock("lock", "theirs", 60)
        assert time.time() + 59 < int(backend.client.items["lock"]["ttl"]["N"]) <= time.time() + 61
        await backend.release_lock("lock", "mine")
        await backend.release_lock("lock", "mine")  # already released
        assert await backend.acquire_lock("lock", "theirs", 5)
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now + 6)  # expired
        assert not await backend.extend_lock("lock", "theirs", 5)
        assert await backend.acquire_lock("lock", "mine", 5)

    @pytest.mark.parametrize("operation", ["put_item", "delete_item", "update_item"])
    async def test_other_lock_errors_go_through(self, backend: Any, operation: str) -> None:
        async def throttled(**kwargs: Any) -> Any:
            error = {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}
            raise ClientError(error, operation)  # type: ignore[arg-type]

        setattr(backend.client, operation, throttled)
        calls: dict[str, Callable[[], Awaitable[Any]]] = {
            "put_item": lambda: backend.acquire_lock("lock", "mine", 5),
            "delete_item": lambda: backend.release_lock("lock", "mine"),
            "update_item": lambda: backend.extend_lock("lock", "mine", 5),
        }
        with pytest.raises(ClientError):
            await calls[operation]()

    async def test_other_errors_go_through(self, backend: Any) -> None:
        async def throttled(**kwargs: Any) -> Any:
            error = {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}
            raise ClientError(error, "UpdateItem")  # type: ignore[arg-type]

        backend.client.update_item = throttled
        with pytest.raises(ClientError):
            await backend.incr_window("k:1", "k:0", amount=1, limit=5, weight=1.0, expire=60)

    async def test_expired_counters_still_in_the_table_are_ignored(self, backend: Any) -> None:
        await backend.incr("c", 5, expire=60)
        backend.client.items["c"]["ttl"]["N"] = str(int(time.time()) - 1)
        assert await backend.get_counter("c") == 0

    async def test_needs_init(self) -> None:
        from fastapi_alcazaba.backends.dynamodb import DynamoBackend

        backend = DynamoBackend(table_name="cache")
        with pytest.raises(RuntimeError, match="init"):
            await backend.get("k")
        await backend.close()

    async def test_close(self, backend: Any) -> None:
        await backend.close()
        assert backend.client is None


class TestInMemoryState:
    """Rate-limit counters, revoked tokens and the like are kept apart from cached responses."""

    async def test_caching_many_responses_never_evicts_state(self) -> None:
        backend = InMemoryBackend(max_size=2)
        await backend.incr("app!ratelimit:{a}", 1, 60)
        await backend.set("app!revoked:token:x", b"1", 60)
        for i in range(10):
            await backend.set(f"app:responses:{i}", b"v", 60)
        assert await backend.get_counter("app!ratelimit:{a}") == 1
        assert await backend.get("app!revoked:token:x") == b"1"
        assert list(backend._store) == ["app:responses:8", "app:responses:9"]

    async def test_expired_state_goes_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = InMemoryBackend(max_size=2)
        await backend.set("app!otp:short", b"1", 1)
        await backend.set("app!otp:long", b"1", 600)
        later = time.monotonic() + 5
        monkeypatch.setattr(time, "monotonic", lambda: later)
        await backend.set("app!otp:new", b"1", 600)
        assert list(backend._state) == ["app!otp:long", "app!otp:new"]

    async def test_expired_state_is_looked_for_once_a_second(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend, clock = InMemoryBackend(max_size=3), [time.monotonic()]
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])
        await backend.set("app!otp:x", b"1", 600)
        await backend.set("app!otp:a", b"1", 1)
        await backend.set("app!otp:y", b"1", 600)
        clock[0] += 0.5
        await backend.set("app!otp:b", b"1", 600)  # looks for expired keys: none, x goes
        await backend.get("app!otp:a")  # not the least recently used any more
        clock[0] += 0.6  # a has expired
        await backend.set("app!otp:c", b"1", 600)  # too soon to look again: y goes
        assert list(backend._state) == ["app!otp:b", "app!otp:a", "app!otp:c"]

    async def test_live_state_is_bounded_too(self) -> None:
        backend = InMemoryBackend(max_size=2)
        for i in range(3):
            await backend.set(f"app!otp:{i}", b"1", 600)
        assert list(backend._state) == ["app!otp:1", "app!otp:2"]

    async def test_clearing_a_namespace(self) -> None:
        backend = InMemoryBackend()
        for key in ("app:ns:1", "app!otp:1", "app!otp:2"):
            await backend.set(key, b"1", 60)
        assert await backend.clear(namespace="app") == 1  # the response, not the state
        assert await backend.clear(namespace="app!otp") == 2

    @pytest.mark.parametrize(
        ("key", "state"),
        [
            ("app!lock:{x}", True),
            ("!revoked:token:x", True),
            ("app:responses:x", False),
            (":responses:x", False),
            ("app:responses:hello!", False),
            ("no-colon!", False),
        ],
    )
    def test_which_keys_are_state(self, key: str, state: bool) -> None:
        assert is_state_key(key) is state
