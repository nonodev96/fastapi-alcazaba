# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

import fakeredis
import pytest
from aiomcache.exceptions import ClientException
from botocore.exceptions import ClientError

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends import Backend
from fastapi_alcazaba.backends._generations import MAGIC as STAMP_MAGIC
from fastapi_alcazaba.backends._generations import (
    Stamp,
    alive,
    encode_stamp,
    namespace_generation,
    namespaces,
    parse_stamp,
)
from fastapi_alcazaba.backends._redis_common import RedisLikeBackend, escape_glob
from fastapi_alcazaba.backends.base import is_state_key
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.memcached import MemcachedBackend, memcached_key
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.backends.valkey import ValkeyBackend
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.response_cache import cache


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


@pytest.fixture(name="fake_redis")
def fixture_fake_redis() -> Any:
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
        """A namespace pasted into a Lua script would run FLUSHALL."""
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

    async def test_clear_the_empty_namespace(self, fake_redis: Any) -> None:
        backend = RedisBackend(fake_redis)
        for key in ("::1", ":ns:1", "!lock", "other"):
            await fake_redis.set(key, b"x")
        assert await backend.clear(namespace="") == 2
        assert sorted(await fake_redis.keys()) == [b"!lock", b"other"]

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
    """memcached's commands as aiomcache sends them: expiry (relative, or a Unix time beyond 30
    days), CAS tokens, multi-get."""

    def __init__(self) -> None:
        self.data: dict[bytes, bytes] = {}
        self.exptimes: dict[bytes, int] = {}
        self.deadlines: dict[bytes, float] = {}
        self.cas_tokens: dict[bytes, int] = {}
        self._writes = 0

    def _live(self, key: bytes) -> bool:
        deadline = self.deadlines.get(key)
        if key in self.data and deadline is not None and deadline <= time.time():
            del self.data[key]
        return key in self.data

    async def get(self, key: bytes) -> bytes | None:
        return self.data[key] if self._live(key) else None

    async def multi_get(self, *keys: bytes) -> tuple[bytes | None, ...]:
        assert len(set(keys)) == len(keys), "aiomcache refuses duplicate keys"
        return tuple([await self.get(key) for key in keys])

    async def gets(self, key: bytes) -> tuple[bytes | None, int | None]:
        if not self._live(key):
            return None, None
        return self.data[key], self.cas_tokens.get(key, 0)

    async def set(self, key: bytes, value: bytes, exptime: int = 0) -> bool:
        assert exptime >= 0, "aiomcache refuses a negative exptime"
        self._writes += 1
        self.data[key] = value
        self.exptimes[key] = exptime
        self.cas_tokens[key] = self._writes
        if not exptime:
            self.deadlines.pop(key, None)
        else:
            relative = exptime <= 30 * 24 * 3600
            self.deadlines[key] = time.time() + exptime if relative else exptime
        return True

    async def cas(self, key: bytes, value: bytes, cas_token: int, exptime: int = 0) -> bool:
        if not self._live(key) or self.cas_tokens.get(key, 0) != cas_token:
            return False
        return await self.set(key, value, exptime)

    async def delete(self, key: bytes) -> bool:
        return self._live(key) and self.data.pop(key, None) is not None

    async def incr(self, key: bytes, increment: int = 1) -> int:
        return self._change(b"incr", key, increment)

    async def decr(self, key: bytes, decrement: int = 1) -> int:
        return self._change(b"decr", key, -decrement)

    def _change(self, command: bytes, key: bytes, delta: int) -> int:
        if not self._live(key):  # what aiomcache 0.8 does, though it documents returning None
            raise ClientException(f"Memcached {command!s} command failed", bytearray(b"NOT_FOUND"))
        if not self.data[key].isdigit():
            raise ClientException(
                f"Memcached {command!s} command failed", bytearray(b"CLIENT_ERROR")
            )
        value = max(0, int(self.data[key]) + delta)  # memcached stops at 0
        self.data[key] = str(value).encode()
        return value

    async def add(self, key: bytes, value: bytes, exptime: int = 0) -> bool:
        if self._live(key):
            return False
        return await self.set(key, value, exptime)


class RacingMemcache(FakeMemcache):
    """Another client creates the counter between our incr and our add."""

    async def add(self, key: bytes, value: bytes, exptime: int = 0) -> bool:
        if key not in self.data:
            self.data[key] = b"10"
        return await super().add(key, value, exptime)


class TestGenerations:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("app:ns:abc", ["app", "app:ns"]),
            ("::abc", ["", ":"]),
            (":ns:abc", ["", ":ns"]),
            ("no-colon", []),
            ("app!lock:{x}", []),  # state
            (":".join("abcdefghijk"), [":".join("abcdefghijk"[:n]) for n in range(1, 9)]),
        ],
    )
    def test_namespaces_of_a_key(self, key: str, expected: list[str]) -> None:
        assert namespaces(key) == expected

    def test_stamp_round_trip(self) -> None:
        stamp = Stamp(1_800_000_000, {"g1": "a", "g2": ""})
        data = encode_stamp(stamp, b"\x00value\nwith new lines")
        assert parse_stamp(data) == (stamp, b"\x00value\nwith new lines")
        assert parse_stamp(b'{"json": "of 0.7"}') is None

    @pytest.mark.parametrize(
        "line",
        [
            b"[]",
            b'{"e": 0}',
            b'{"e": 0, "g": {}, "x": 1}',
            b'{"e": 1.5, "g": {}}',
            b'{"e": true, "g": {}}',
            b'{"e": -1, "g": {}}',
            b'{"e": 0, "g": []}',
            b'{"e": 0, "g": {"a": 1}}',
            b"\xff",
            b"[" * 100_000,
        ],
    )
    def test_malformed_stamps(self, line: bytes) -> None:
        with pytest.raises(ValueError, match="."):
            parse_stamp(STAMP_MAGIC + line + b"\nvalue")

    @pytest.mark.parametrize(
        ("recorded", "current", "is_alive"),
        [
            ({"a": "1"}, {"a": "1"}, True),
            ({"a": "1"}, {"a": "2"}, False),
            ({"a": "1"}, {"a": None}, False),  # evicted
            ({"a": ""}, {"a": None}, True),
            ({}, {"a": None}, True),
            ({}, {"a": "1"}, False),  # cleared since
            ({"tag": "1"}, {}, False),  # not fetched: none now
        ],
    )
    def test_alive(
        self,
        recorded: dict[str, str],
        current: dict[str, str | None],
        is_alive: bool,
    ) -> None:
        assert alive(Stamp(0, recorded), current, ["a"]) is is_alive


class CountingMemcache(FakeMemcache):
    """Counts the round trips of reads."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def multi_get(self, *keys: bytes) -> tuple[bytes | None, ...]:
        self.reads += 1
        return await super().multi_get(*keys)


class OvertakingMemcache(FakeMemcache):
    """Another client writes the key right after each gets."""

    async def gets(self, key: bytes) -> tuple[bytes | None, int | None]:
        found = await super().gets(key)
        if found[0] is not None:
            await self.set(key, b"theirs", self.exptimes[key])
        return found


def memcached(mcache: FakeMemcache | None = None) -> MemcachedBackend:
    return MemcachedBackend(mcache or FakeMemcache())  # type: ignore[arg-type]


class TestMemcachedBackend:
    async def test_set_get_and_ttl(self) -> None:
        backend = memcached()
        await backend.set("app:ns:k", b"v", expire=60)
        ttl, value = await backend.get_with_ttl("app:ns:k")
        assert (value, 59 <= ttl <= 60) == (b"v", True)
        await backend.set("app:ns:forever", b"v")
        assert await backend.get_with_ttl("app:ns:forever") == (-1, b"v")
        assert await backend.get_with_ttl("missing") == (0, None)

    async def test_lifetime_counts_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = memcached()
        await backend.set("app:ns:k", b"v", expire=60)
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now + 50)
        assert await backend.get_with_ttl("app:ns:k") == (10, b"v")
        monkeypatch.setattr(time, "time", lambda: now + 61)
        assert await backend.get_with_ttl("app:ns:k") == (0, None)

    async def test_state_is_stored_as_is(self) -> None:
        """Locks, counters and sessions read the same by every version."""
        mcache = FakeMemcache()
        backend = memcached(mcache)
        await backend.set("app!session:x", b"v", expire=60)
        assert mcache.data[b"app!session:x"] == b"v"
        assert await backend.get_with_ttl("app!session:x") == (-1, b"v")
        assert await backend.get("app!session:missing") is None

    async def test_long_expire_becomes_a_timestamp(self) -> None:
        mcache = FakeMemcache()
        backend = memcached(mcache)
        await backend.set("short", b"v", expire=60)
        await backend.set("long", b"v", expire=40 * 24 * 3600)
        assert mcache.exptimes[b"short"] == 60
        assert mcache.exptimes[b"long"] > time.time()
        assert 40 * 24 * 3600 - 2 <= (await backend.get_with_ttl("long"))[0] <= 40 * 24 * 3600

    def test_invalid_keys_are_hashed(self) -> None:
        assert memcached_key("app:ns:abc") == b"app:ns:abc"
        for bad in ("with space", "x" * 300, "ñ", "tab\t"):
            hashed = memcached_key(bad)
            assert hashed.startswith(b"fastapi_alcazaba:sha256:")
            assert len(hashed) <= 250

    async def test_clear_a_key(self) -> None:
        backend = memcached()
        await backend.set("k", b"v")
        assert await backend.clear(key="k") == 1
        assert await backend.clear(key="k") == 0
        assert await backend.clear() == 0

    async def test_clear_a_namespace(self) -> None:
        backend = memcached()
        for key in ("app:a:1", "app:a:2", "app:ab:1", "app!otp:a"):
            await backend.set(key, b"v", expire=60)
        assert await backend.clear(namespace="app:a") == 0  # can't count them
        assert [await backend.get(key) for key in ("app:a:1", "app:a:2")] == [None, None]
        assert await backend.get("app:ab:1") == b"v"
        await backend.set("app:a:1", b"new", expire=60)
        assert await backend.get("app:a:1") == b"new"  # stored after the clear
        await backend.clear(namespace="app")
        assert await backend.get("app:ab:1") is None
        assert await backend.get("app:a:1") is None
        assert await backend.get("app!otp:a") == b"v"  # state isn't in any namespace

    async def test_clear_everything_without_a_prefix(self) -> None:
        backend = memcached()
        FastAPICache.init(backend)
        for key in ("::1", ":ns:1", "other-app:1"):
            await backend.set(key, b"x")
        assert await FastAPICache.clear() == 0
        assert [await backend.get(key) for key in ("::1", ":ns:1")] == [None, None]
        assert await backend.get("other-app:1") == b"x"

    async def test_too_deep_a_namespace_is_refused(self) -> None:
        backend = memcached()
        await backend.clear(namespace=":".join("abcdefgh"))  # 7 colons
        with pytest.raises(ValueError, match="fewer than 8"):
            await backend.clear(namespace=":".join("abcdefghi"))

    async def test_a_clear_while_storing_hides_the_entry(self) -> None:
        """The generations are read before the value is written: a clear in between wins."""
        backend = memcached()
        read_generations = backend._generations

        async def cleared_meanwhile(names: Any) -> dict[str, str]:
            tokens = await read_generations(names)
            await backend.clear(namespace="app:ns")
            return tokens

        backend._generations = cleared_meanwhile  # type: ignore[method-assign]
        await backend.set("app:ns:1", b"computed before the clear", expire=60)
        backend._generations = read_generations  # type: ignore[method-assign]
        assert await backend.get("app:ns:1") is None

    async def test_an_evicted_generation_never_brings_entries_back(self) -> None:
        mcache = FakeMemcache()
        backend = memcached(mcache)
        await backend.set("app:ns:1", b"v", expire=60)
        await backend.clear(namespace="app:ns")
        del mcache.data[memcached_key(namespace_generation("app:ns"))]
        assert await backend.get("app:ns:1") is None
        await backend.set("app:ns:2", b"v", expire=60)
        del mcache.data[memcached_key(namespace_generation("app"))]
        assert await backend.get("app:ns:2") is None  # recomputed, never stale

    async def test_a_generation_created_by_another_client(self) -> None:
        """Two first stores at once: both record the generation one of them created."""
        mcache = FakeMemcache()
        backend = memcached(mcache)
        add = mcache.add

        async def overtaken(key: bytes, value: bytes, exptime: int = 0) -> bool:
            await add(key, b"theirs", exptime)
            return await add(key, value, exptime)

        mcache.add = overtaken  # type: ignore[method-assign]
        await backend.set("app:1", b"v")
        assert await backend.get("app:1") == b"v"
        assert mcache.data[memcached_key(namespace_generation("app"))] == b"theirs"

    async def test_entries_of_earlier_versions_are_misses(self) -> None:
        """Whether a clear hid them since can't be told."""
        mcache = FakeMemcache()
        backend = memcached(mcache)
        mcache.data[b"app:ns:1"] = b'{"written": "by 0.7"}'
        assert await backend.get_with_ttl("app:ns:1") == (0, None)

    @pytest.mark.parametrize(
        "stored",
        [
            STAMP_MAGIC + b'{"e": 0, "g": {}}',  # no end of line
            STAMP_MAGIC + b"not json\nv",
            STAMP_MAGIC + b'{"e": -1, "g": {}}\nv',
        ],
    )
    async def test_unreadable_entries_are_misses(
        self,
        stored: bytes,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mcache = FakeMemcache()
        backend = memcached(mcache)
        mcache.data[b"app:ns:1"] = stored
        assert await backend.get_with_ttl("app:ns:1") == (0, None)
        assert "unreadable entry 'app:ns:1'" in caplog.text
        await backend.add_tags("app:ns:1", ["app:tag:{t}"], 60)  # left as it is
        assert mcache.data[b"app:ns:1"] == stored

    async def test_locks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """add takes it; gets + cas release or extend it, only for its holder."""
        mcache = FakeMemcache()
        backend = memcached(mcache)
        assert backend.supports_locks
        assert await backend.acquire_lock("app!lock:x", "mine", 5)
        assert not await backend.acquire_lock("app!lock:x", "theirs", 5)
        await backend.release_lock("app!lock:x", "theirs")
        assert not await backend.extend_lock("app!lock:x", "theirs", 60)
        assert not await backend.acquire_lock("app!lock:x", "theirs", 5)
        assert await backend.extend_lock("app!lock:x", "mine", 60)
        assert mcache.exptimes[b"app!lock:x"] == 61
        await backend.release_lock("app!lock:x", "mine")
        await backend.release_lock("app!lock:x", "mine")  # already released
        assert not await backend.extend_lock("app!lock:x", "mine", 5)
        assert await backend.acquire_lock("app!lock:x", "theirs", 5)
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now + 7)  # expired
        assert not await backend.extend_lock("app!lock:x", "theirs", 5)
        assert await backend.acquire_lock("app!lock:x", "mine", 5)

    @pytest.mark.parametrize(
        ("ttl", "exptime"), [(0.05, 2), (1, 2), (2.5, 4), (30 * 24 * 3600, 30 * 24 * 3600 + 1)]
    )
    async def test_locks_last_whole_seconds_never_less(self, ttl: float, exptime: int) -> None:
        mcache = FakeMemcache()
        assert await memcached(mcache).acquire_lock("lock", "mine", ttl)
        if exptime > 30 * 24 * 3600:  # a Unix time
            exptime += int(time.time())
        assert abs(mcache.exptimes[b"lock"] - exptime) <= 1

    async def test_a_release_expires_the_lock_at_once(self) -> None:
        mcache = FakeMemcache()
        backend = memcached(mcache)
        await backend.acquire_lock("lock", "mine", 60)
        await backend.release_lock("lock", "mine")
        assert mcache.exptimes[b"lock"] == 30 * 24 * 3600 + 1  # a Unix time in 1970
        assert await mcache.get(b"lock") is None

    async def test_a_lock_written_since_isnt_released_nor_extended(self) -> None:
        mcache = OvertakingMemcache()
        backend = memcached(mcache)
        await backend.acquire_lock("lock", "mine", 60)
        await backend.release_lock("lock", "mine")
        assert mcache.data[b"lock"] == b"theirs"
        await mcache.set(b"lock", b"mine", 60)
        assert not await backend.extend_lock("lock", "mine", 60)

    async def test_tags(self) -> None:
        backend = memcached()
        assert backend.supports_tags
        tag, other = "app:tag:{a}", "app:tag:{b}"
        for i in range(3):
            await backend.set(f"app:ns:{i}", b"v", expire=60)
            await backend.add_tags(f"app:ns:{i}", [tag] if i < 2 else [other], 60)
        assert await backend.invalidate_tags([tag]) == 0  # can't count them
        assert [await backend.get(f"app:ns:{i}") for i in range(3)] == [None, None, b"v"]
        assert await backend.invalidate_tags_with_keys([other]) == (0, None)
        assert await backend.get("app:ns:2") is None
        await backend.set("app:ns:0", b"new", expire=60)
        await backend.add_tags("app:ns:0", [tag], 60)
        assert await backend.get("app:ns:0") == b"new"  # stored after the invalidation

    async def test_tagging_keeps_the_expiry(self) -> None:
        mcache = FakeMemcache()
        backend = memcached(mcache)
        await backend.set("app:ns:1", b"v", expire=60)
        await backend.add_tags("app:ns:1", ["app:tag:{a}"], 60)
        assert mcache.exptimes[b"app:ns:1"] > time.time()  # the entry's expiry, a Unix time
        assert 59 <= (await backend.get_with_ttl("app:ns:1"))[0] <= 60
        await backend.set("app:ns:2", b"v")
        await backend.add_tags("app:ns:2", ["app:tag:{a}"], None)
        assert mcache.exptimes[b"app:ns:2"] == 0

    async def test_tagging_what_isnt_there_does_nothing(self) -> None:
        mcache = FakeMemcache()
        backend = memcached(mcache)
        await backend.add_tags("app:ns:missing", ["app:tag:{a}"], 60)
        mcache.data[b"app:ns:old"] = b"by 0.7"
        await backend.add_tags("app:ns:old", ["app:tag:{a}"], 60)
        assert mcache.data[b"app:ns:old"] == b"by 0.7"
        assert b"app:ns:missing" not in mcache.data

    async def test_a_newer_value_isnt_overwritten_by_tagging(self) -> None:
        """Written between the gets and the cas: its own writer tags it."""
        mcache = OvertakingMemcache()
        backend = memcached(mcache)
        await backend.set("app:ns:1", b"mine", expire=60)
        await backend.add_tags("app:ns:1", ["app:tag:{a}"], 60)
        assert mcache.data[b"app:ns:1"] == b"theirs"

    async def test_round_trips_of_a_hit(self) -> None:
        """One for an entry, two for a tagged one."""
        mcache = CountingMemcache()
        backend = memcached(mcache)
        await backend.set("app:ns:1", b"v", expire=60)
        await backend.set("app:ns:2", b"v", expire=60)
        await backend.add_tags("app:ns:2", ["app:tag:{a}"], 60)
        mcache.reads = 0
        await backend.get("app:ns:1")
        assert mcache.reads == 1
        await backend.get("app:ns:2")
        assert mcache.reads == 3

    async def test_early_refresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hits know their remaining lifetime, so early refresh happens."""
        FastAPICache.init(memcached(), early_refresh=0.5)
        calls: list[int] = []

        @cache(expire=100)
        async def value() -> int:
            calls.append(1)
            return len(calls)

        assert await value() == 1
        monkeypatch.setattr(random, "random", lambda: 0.99)
        assert await value() == 1  # 100 s left of 100: outside the window
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now + 90)  # 10 s left: inside
        assert await value() == 2

    async def test_counters(self) -> None:
        mcache = FakeMemcache()
        backend = memcached(mcache)
        assert backend.supports_counters
        assert await backend.get_counter("c") == 0
        assert await backend.incr("c", expire=60) == 1
        assert mcache.exptimes[b"c"] == 60
        assert await backend.incr("c", 3, expire=60) == 4
        assert await backend.incr("c", -1, expire=60) == 3
        assert await backend.get_counter("app!c") == 0
        assert await backend.incr("app!c", 3, expire=60) == 3
        assert await backend.get_counter("app!c") == 3
        assert await backend.incr("new", -1, expire=60) == 0  # no negative counters

    async def test_incr_window_adds_and_takes_back(self) -> None:
        """memcached has no conditional increment: Backend's default."""
        await check_incr_window(memcached())

    async def test_counter_created_by_another_client(self) -> None:
        backend = memcached(RacingMemcache())
        assert await backend.incr("c", expire=60) == 11

    async def test_other_errors_go_through(self) -> None:
        backend = memcached()
        await backend.set("app!not a number", b"text")
        with pytest.raises(ClientException):
            await backend.incr("app!not a number")


class TestFastAPICacheClear:
    async def test_clear_key_with_a_prefix_only_deletes_that_key(self) -> None:
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

    async def test_clear_everything_without_a_prefix(self) -> None:
        """The default prefix is "": its keys start with ":", and clear() deleted none."""
        backend = InMemoryBackend()
        FastAPICache.init(backend)
        await backend.set("::1", b"x")
        await backend.set(":ns:1", b"x")
        await backend.set("other-app:1", b"x")
        assert await FastAPICache.clear() == 2
        assert await backend.get("other-app:1") == b"x"


class FakeDynamoClient:
    """The calls DynamoBackend makes, on one table: TableName and the like are left out."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.requests = 0
        self.unprocessed = 0  # BatchGetItems that process no key

    async def get_item(self, Key: dict[str, Any], **_: Any) -> dict[str, Any]:  # noqa: N803  # pylint: disable=invalid-name
        self.requests += 1
        item = self.items.get(Key["key"]["S"])
        return {} if item is None else {"Item": item}

    async def batch_get_item(self, RequestItems: dict[str, Any]) -> dict[str, Any]:  # noqa: N803  # pylint: disable=invalid-name
        self.requests += 1
        ((table, request),) = RequestItems.items()
        keys = [key["key"]["S"] for key in request["Keys"]]
        assert len(set(keys)) == len(keys) <= 100, "DynamoDB refuses duplicate keys"
        if self.unprocessed:
            self.unprocessed -= 1
            return {"Responses": {table: []}, "UnprocessedKeys": RequestItems}
        found = [self.items[key] for key in keys if key in self.items]
        return {"Responses": {table: found}, "UnprocessedKeys": {}}

    async def put_item(self, Item: dict[str, Any], **kwargs: Any) -> None:  # noqa: N803  # pylint: disable=invalid-name
        key = Item["key"]["S"]
        if "ConditionExpression" in kwargs:  # only DynamoBackend.acquire_lock's
            assert kwargs["ConditionExpression"] == "attribute_not_exists(#key) OR #expires < :now"
            existing = self.items.get(key)
            now = int(kwargs["ExpressionAttributeValues"][":now"]["N"])
            if existing is not None and int(existing["lock_expires"]["N"]) >= now:
                conditional_check_failed()
        self.items[key] = Item

    async def delete_item(self, Key: dict[str, Any], **kwargs: Any) -> Any:  # noqa: N803  # pylint: disable=invalid-name
        key = Key["key"]["S"]
        existing = self.items.get(key)
        if kwargs["ConditionExpression"] == "attribute_exists(#key)":  # DynamoBackend.clear's
            if existing is None:
                conditional_check_failed()
        else:  # DynamoBackend.release_lock's
            assert kwargs["ConditionExpression"] == "#token = :token"
            token = kwargs["ExpressionAttributeValues"][":token"]
            if existing is None or existing["token"] != token:
                conditional_check_failed()
        del self.items[key]
        return {}

    async def update_item(self, **kwargs: Any) -> dict[str, Any]:
        """Only what DynamoBackend sends: ADD to a number, SET if_not_exists, and the condition
        "attribute_not_exists(#count) OR #count <= :at_most"; or extend_lock's SET."""
        names, values = kwargs["ExpressionAttributeNames"], kwargs["ExpressionAttributeValues"]
        if kwargs["UpdateExpression"] == "SET #tags = :tags":  # add_tags's
            assert kwargs["ConditionExpression"] == "attribute_exists(#key)"
            item = self.items.get(kwargs["Key"]["key"]["S"])
            if item is None:
                conditional_check_failed()
            item[names["#tags"]] = values[":tags"]
            return {}
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
        """A conditional delete: of several deleting an entry at once, one is told it was there."""
        await backend.set("k", b"v")
        assert await backend.clear(key="k") == 1
        assert await backend.clear(key="k") == 0
        assert await backend.clear() == 0

    async def test_clear_a_namespace(self, backend: Any) -> None:
        for key in ("app:a:1", "app:a:2", "app:ab:1", "app!otp:a"):
            await backend.set(key, b"v", expire=60)
        assert await backend.clear(namespace="app:a") == 0  # can't count them
        assert [await backend.get(key) for key in ("app:a:1", "app:a:2")] == [None, None]
        assert await backend.get("app:ab:1") == b"v"
        await backend.set("app:a:1", b"new", expire=60)
        assert await backend.get("app:a:1") == b"new"  # stored after the clear
        await backend.clear(namespace="app")
        assert await backend.get("app:ab:1") is None
        assert await backend.get("app:a:1") is None
        assert await backend.get("app!otp:a") == b"v"  # state isn't in any namespace
        with pytest.raises(ValueError, match="fewer than 8"):
            await backend.clear(namespace=":".join("abcdefghi"))

    async def test_a_clear_while_storing_hides_the_entry(self, backend: Any) -> None:
        read_generations = backend._generations

        async def cleared_meanwhile(names: Any) -> dict[str, str]:
            tokens: dict[str, str] = await read_generations(names)
            await backend.clear(namespace="app:ns")
            return tokens

        await backend.clear(namespace="app:ns")
        backend._generations = cleared_meanwhile
        await backend.set("app:ns:1", b"computed before the clear", expire=60)
        backend._generations = read_generations
        assert await backend.get("app:ns:1") is None

    async def test_state_and_items_without_a_value(self, backend: Any) -> None:
        await backend.set("app!session:x", b"v", expire=60)
        assert "generations" not in backend.client.items["app!session:x"]
        assert (await backend.get_with_ttl("app!session:x"))[1] == b"v"
        assert await backend.get("app!session:missing") is None
        await backend.incr("app:ns:counter", 1)  # an item without a value
        assert await backend.get("app:ns:counter") is None

    async def test_items_of_earlier_versions(self, backend: Any) -> None:
        """Without generations: served until their namespace is cleared."""
        backend.client.items["app:ns:1"] = {"key": {"S": "app:ns:1"}, "value": {"B": b"v"}}
        assert await backend.get_with_ttl("app:ns:1") == (-1, b"v")
        await backend.clear(namespace="app")
        assert await backend.get("app:ns:1") is None

    async def test_tags(self, backend: Any) -> None:
        assert backend.supports_tags
        tag, other = "app:tag:{a}", "app:tag:{b}"
        for i in range(3):
            await backend.set(f"app:ns:{i}", b"v", expire=60)
            await backend.add_tags(f"app:ns:{i}", [tag] if i < 2 else [other], 60)
        assert await backend.invalidate_tags([tag]) == 0  # can't count them
        assert [await backend.get(f"app:ns:{i}") for i in range(3)] == [None, None, b"v"]
        assert await backend.invalidate_tags_with_keys([other]) == (0, None)
        assert await backend.get("app:ns:2") is None
        await backend.set("app:ns:0", b"new", expire=60)
        await backend.add_tags("app:ns:0", [tag], 60)
        assert await backend.get("app:ns:0") == b"new"  # stored after the invalidation

    async def test_tagging_what_isnt_there_does_nothing(self, backend: Any) -> None:
        await backend.add_tags("app:ns:missing", ["app:tag:{a}"], 60)
        assert "app:ns:missing" not in backend.client.items

    async def test_requests_of_a_hit(self, backend: Any) -> None:
        """One for an entry, two for a tagged one."""
        await backend.set("app:ns:1", b"v", expire=60)
        await backend.set("app:ns:2", b"v", expire=60)
        await backend.add_tags("app:ns:2", ["app:tag:{a}"], 60)
        backend.client.requests = 0
        await backend.get("app:ns:1")
        assert backend.client.requests == 1
        await backend.get("app:ns:2")
        assert backend.client.requests == 3

    async def test_unprocessed_keys_are_retried(
        self,
        backend: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        waits: list[float] = []

        async def wait(seconds: float) -> None:
            waits.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", wait)
        await backend.set("app:ns:1", b"v", expire=60)
        backend.client.unprocessed = 2
        assert await backend.get("app:ns:1") == b"v"
        backend.client.unprocessed = 5
        with pytest.raises(BackendUnavailable, match="unprocessed"):
            await backend.get("app:ns:1")
        assert waits == [0.05, 0.1] + [0.05, 0.1, 0.2, 0.4, 0.8]

    async def test_other_tagging_errors_go_through(self, backend: Any) -> None:
        async def throttled(**_: Any) -> Any:
            error = {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}
            raise ClientError(error, "UpdateItem")  # type: ignore[arg-type]

        await backend.set("app:ns:1", b"v", expire=60)
        backend.client.update_item = throttled
        with pytest.raises(ClientError):
            await backend.add_tags("app:ns:1", ["app:tag:{a}"], 60)

    async def test_other_clear_errors_go_through(self, backend: Any) -> None:
        async def throttled(**_: Any) -> Any:
            error = {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}
            raise ClientError(error, "DeleteItem")  # type: ignore[arg-type]

        backend.client.delete_item = throttled
        with pytest.raises(ClientError):
            await backend.clear(key="k")

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
        async def throttled(**_: Any) -> Any:
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
        async def throttled(**_: Any) -> Any:
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

    async def test_live_state_is_never_evicted(self) -> None:
        """A forgotten revocation would accept the token again: a full kind refuses writes."""
        backend = InMemoryBackend(max_size=2)
        await backend.set("app!otp:0", b"1", 600)
        await backend.set("app!otp:1", b"1", 600)
        with pytest.raises(BackendUnavailable, match="'otp' is full"):
            await backend.set("app!otp:2", b"1", 600)
        with pytest.raises(BackendUnavailable):
            await backend.incr("app!otp:3", 1, 600)
        assert list(backend._state["otp"]) == ["app!otp:0", "app!otp:1"]
        await backend.set("app!otp:1", b"2", 600)  # rewriting a key takes no room
        assert await backend.get("app!otp:1") == b"2"

    async def test_a_flood_of_one_kind_leaves_the_others(self) -> None:
        backend = InMemoryBackend(max_size=2)
        await backend.set("app!revoked:token:x", b"1", 600)
        for i in range(2):
            await backend.set(f"app!idempotency:{{{i}}}", b"1", 600)
        with pytest.raises(BackendUnavailable):
            await backend.set("app!idempotency:{2}", b"1", 600)
        assert await backend.get("app!revoked:token:x") == b"1"
        await backend.set("app!revoked:token:y", b"1", 600)

    async def test_expired_state_makes_room(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = InMemoryBackend(max_size=2)
        await backend.set("app!otp:short", b"1", 1)
        await backend.set("app!otp:long", b"1", 600)
        later = time.monotonic() + 5
        monkeypatch.setattr(time, "monotonic", lambda: later)
        await backend.set("app!otp:new", b"1", 600)
        assert list(backend._state["otp"]) == ["app!otp:long", "app!otp:new"]

    async def test_expired_state_is_looked_for_once_a_second(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend, clock = InMemoryBackend(max_size=2), [time.monotonic()]
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])
        await backend.set("app!otp:a", b"1", 1)
        await backend.set("app!otp:x", b"1", 600)
        clock[0] += 0.5
        with pytest.raises(BackendUnavailable):
            await backend.set("app!otp:b", b"1", 600)  # looks for expired keys: none
        clock[0] += 0.7  # a has expired, but they were looked for 0.7 s ago
        with pytest.raises(BackendUnavailable):
            await backend.set("app!otp:c", b"1", 600)
        clock[0] += 0.4
        await backend.set("app!otp:c", b"1", 600)
        assert list(backend._state["otp"]) == ["app!otp:x", "app!otp:c"]

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
