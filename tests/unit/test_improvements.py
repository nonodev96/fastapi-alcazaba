import asyncio
import random
import warnings
from collections.abc import Sequence
from typing import Annotated, Any

import fakeredis
import pytest
from fastapi import Depends, FastAPI
from starlette.requests import Request
from starlette.testclient import TestClient

from fastapi_alcazaba import Cache, FastAPICache, UnstableCacheKeyWarning
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.memcached import MemcachedBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.coder import JsonCoder
from fastapi_alcazaba.decorator import cache
from fastapi_alcazaba.key_builder import default_key_builder
from fastapi_alcazaba.limiter import FastAPIRateLimiter
from fastapi_alcazaba.tokens import TokenBlocklist
from fastapi_alcazaba.types import Backend


class Session:
    """Like a database session: the default repr(), with a memory address."""


# Kept alive: otherwise CPython may give the next session the address of the previous one, and
# the second request would hit by chance (as it sometimes did in backend_fastapi).
_sessions: list[Session] = []


def get_session() -> Session:
    _sessions.append(Session())
    return _sessions[-1]


class TestUnstableKeys:
    def test_warns_about_arguments_with_a_memory_address(self) -> None:
        """The bug that made backend_fastapi's product endpoints miss on every request."""
        FastAPICache.init(InMemoryBackend())
        app = FastAPI()

        @app.get("/products")
        @cache(expire=60)
        def products(session: Annotated[Session, Depends(get_session)], page: int = 1) -> list[int]:
            return [page]

        with TestClient(app) as client:
            with pytest.warns(UnstableCacheKeyWarning, match="session"):
                client.get("/products")
            assert client.get("/products").headers["X-FastAPI-Cache"] == "MISS"

    def test_exclude_params_fixes_it(self) -> None:
        FastAPICache.init(InMemoryBackend())
        app = FastAPI()

        @app.get("/products")
        @cache(expire=60, exclude_params=["session"])
        def products(session: Annotated[Session, Depends(get_session)], page: int = 1) -> list[int]:
            return [page]

        with TestClient(app) as client, warnings.catch_warnings():
            warnings.simplefilter("error")
            client.get("/products")
            assert client.get("/products").headers["X-FastAPI-Cache"] == "HIT"

    async def test_functions_and_methods_count_too(self) -> None:
        FastAPICache.init(InMemoryBackend())

        @cache(expire=60)
        async def run(callback: Any) -> int:
            return 1

        with pytest.warns(UnstableCacheKeyWarning, match="callback"):
            await run(lambda: None)  # type: ignore[no-untyped-call]

    async def test_custom_key_builders_are_trusted(self) -> None:
        def key_builder(func: Any, namespace: str = "", **kwargs: Any) -> str:
            return f"{namespace}:fixed"

        FastAPICache.init(InMemoryBackend(), key_builder=key_builder)

        @cache(expire=60)
        async def run(session: Session) -> int:
            return 1

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            await run(Session())


class TestMaxValueSize:
    async def test_large_values_are_not_stored(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, max_value_size=100)

        @cache(expire=60)
        async def big() -> str:
            return "x" * 200

        @cache(expire=60)
        async def small() -> str:
            return "x"

        assert await big() == "x" * 200
        await small()
        assert len(backend._store) == 1

    async def test_per_endpoint_limit(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend)

        @cache(expire=60, max_value_size=10)
        async def value() -> str:
            return "x" * 20

        await value()
        assert not backend._store

    def test_invalid_limits(self) -> None:
        with pytest.raises(ValueError, match="max_value_size"):
            cache(max_value_size=0)
        with pytest.raises(ValueError, match="max_value_size"):
            FastAPICache.init(InMemoryBackend(), max_value_size=0)


class TestEarlyRefresh:
    async def test_recomputes_near_the_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, early_refresh=0.5)
        calls: list[int] = []

        @cache(expire=100)
        async def value() -> int:
            calls.append(1)
            return len(calls)

        assert await value() == 1
        monkeypatch.setattr(random, "random", lambda: 0.99)
        assert await value() == 1  # 100 s left of 100: outside the window
        for entry in backend._store.values():
            entry.expires_at -= 90  # type: ignore[operator]  # 10 s left: inside the window
        assert await value() == 2
        assert len(calls) == 2

    async def test_unlikely_far_from_the_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, early_refresh=0.5)

        @cache(expire=100)
        async def value() -> int:
            return 1

        await value()
        for entry in backend._store.values():
            entry.expires_at -= 40  # type: ignore[operator]  # 60 s left, window starts at 50
        monkeypatch.setattr(random, "random", lambda: 0.99)
        assert await value() == 1

    async def test_a_failed_refresh_serves_the_cached_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, early_refresh=0.5)
        fail = False

        @cache(expire=100)
        async def value() -> int:
            if fail:
                raise ConnectionError("database down")
            return 1

        await value()
        for entry in backend._store.values():
            entry.expires_at -= 90  # type: ignore[operator]
        monkeypatch.setattr(random, "random", lambda: 0.99)
        fail = True
        assert await value() == 1

    def test_invalid_fraction(self) -> None:
        with pytest.raises(ValueError, match="early_refresh"):
            cache(early_refresh=1.5)


class TestDistributedLock:
    async def test_one_process_computes_while_the_other_waits(self) -> None:
        """Two processes sharing Redis: single_flight is off to stand for separate processes."""
        redis = fakeredis.FakeAsyncRedis()
        FastAPICache.init(RedisBackend(redis), distributed_lock=True, single_flight=False)
        calls: list[int] = []

        @cache(expire=60, vary_headers=())
        async def slow() -> int:
            calls.append(1)
            await asyncio.sleep(0.2)
            return 7

        assert list(await asyncio.gather(slow(), slow())) == [7, 7]
        assert len(calls) == 1
        assert await redis.keys("*:lock") == []

    async def test_waits_at_most_lock_wait(self) -> None:
        redis = fakeredis.FakeAsyncRedis()
        backend = RedisBackend(redis)
        FastAPICache.init(backend, distributed_lock=True, lock_wait=0.1)

        @cache(expire=60)
        async def value() -> int:
            return 1

        # Someone else holds the lock and never stores the value.
        original = backend.acquire_lock

        async def taken(key: str, token: str, ttl: float) -> bool:
            await original(key, "someone-else", ttl)
            return False

        backend.acquire_lock = taken  # type: ignore[method-assign]
        assert await asyncio.wait_for(value(), timeout=1) == 1

    async def test_waiting_callers_reuse_what_the_first_one_waited_for(self) -> None:
        """Another process holds the lock and stores the value: the first caller here waits for
        it, and the ones waiting on that caller (single flight) must reuse it, not compute."""
        redis = fakeredis.FakeAsyncRedis()
        FastAPICache.init(RedisBackend(redis), prefix="p", distributed_lock=True)
        calls: list[int] = []

        async def value() -> int:
            calls.append(1)
            return 1

        cached = cache(expire=60, namespace="ns", vary_headers=())(value)
        key = default_key_builder(value, "p:ns", args=(), kwargs={})
        await redis.set(f"{key}:lock", "other-process", px=10_000)

        async def other_process() -> None:
            await asyncio.sleep(0.1)
            await redis.set(key, JsonCoder.encode(7), ex=60)
            await redis.delete(f"{key}:lock")

        results = await asyncio.gather(other_process(), *(cached() for _ in range(10)))
        assert results[1:] == [7] * 10
        assert calls == []

    async def test_checks_the_cache_again_after_taking_the_lock(self) -> None:
        """The other process stored the value and released the lock between our miss and our
        acquire: we got the lock, but there's nothing to compute."""
        redis = fakeredis.FakeAsyncRedis()
        backend = RedisBackend(redis)
        FastAPICache.init(backend, prefix="p", distributed_lock=True)
        calls: list[int] = []

        async def value() -> int:
            calls.append(1)
            return 1

        cached = cache(expire=60, namespace="ns", vary_headers=())(value)
        key = default_key_builder(value, "p:ns", args=(), kwargs={})
        original = backend.acquire_lock

        async def acquire_after_the_other_process(lock: str, token: str, ttl: float) -> bool:
            await redis.set(key, JsonCoder.encode(7), ex=60)
            return await original(lock, token, ttl)

        backend.acquire_lock = acquire_after_the_other_process  # type: ignore[method-assign,assignment]
        assert await cached() == 7
        assert calls == []
        assert await redis.get(f"{key}:lock") is None  # released

    async def test_backends_without_locks(self) -> None:
        class WithoutLocks(InMemoryBackend):
            supports_locks = False

        with pytest.warns(UserWarning, match="can't hold locks"):
            FastAPICache.init(WithoutLocks(), distributed_lock=True)

        @cache(expire=60)
        async def value() -> int:
            return 1

        assert await value() == 1


class TestTags:
    @pytest.fixture(params=["memory", "redis"])
    def backend(self, request: pytest.FixtureRequest) -> Any:
        return (
            InMemoryBackend()
            if request.param == "memory"
            else RedisBackend(fakeredis.FakeAsyncRedis())
        )

    async def test_invalidate_by_tag(self, backend: Any) -> None:
        FastAPICache.init(backend, prefix="app")
        calls: list[int] = []

        @cache(expire=60, tags=lambda product_id, **_: [f"product:{product_id}"])
        async def product(product_id: int) -> dict[str, int]:
            calls.append(product_id)
            return {"id": product_id}

        @cache(expire=60, tags=["catalog"])
        async def catalog() -> list[int]:
            calls.append(0)
            return [1, 2]

        await product(1)
        await product(2)
        await catalog()
        assert await FastAPICache.invalidate_tags("product:1") == 1
        await product(1)
        await product(2)
        await catalog()
        assert calls == [1, 2, 0, 1]
        assert await FastAPICache.invalidate_tags("catalog", "product:2") == 2
        assert await FastAPICache.invalidate_tags("never-used") == 0

    async def test_tags_that_fail_are_skipped(self) -> None:
        FastAPICache.init(InMemoryBackend())

        @cache(expire=60, tags=lambda **_: [str(1 / 0)])
        async def value() -> int:
            return 1

        assert await value() == 1

    async def test_in_memory_evictions_forget_their_tags(self) -> None:
        backend = InMemoryBackend(max_size=1)
        FastAPICache.init(backend)

        @cache(expire=60, tags=lambda x: [f"t{x}"])
        async def value(x: int) -> int:
            return x

        for x in range(5):
            await value(x)
        assert len(backend._tags) == 1

    async def test_backends_without_tags(self) -> None:
        class FakeMemcache:
            async def get(self, key: bytes) -> bytes | None:
                return None

            async def set(self, key: bytes, value: bytes, exptime: int = 0) -> bool:
                return True

        FastAPICache.init(MemcachedBackend(FakeMemcache()))  # type: ignore[arg-type]

        @cache(expire=60, tags=["x"])
        async def value() -> int:
            return 1

        assert await value() == 1
        with pytest.raises(NotImplementedError):
            await FastAPICache.invalidate_tags("x")

    async def test_tag_sets_live_as_long_as_their_longest_entry(self) -> None:
        redis = fakeredis.FakeAsyncRedis()
        backend = RedisBackend(redis)
        await redis.set("a", b"x")
        await backend.add_tags("a", ["t:{1}"], 10)
        assert 0 < await redis.ttl("t:{1}") <= 10
        await backend.add_tags("a", ["t:{1}"], 100)
        assert await redis.ttl("t:{1}") > 10
        await backend.add_tags("a", ["t:{1}"], 5)
        assert await redis.ttl("t:{1}") > 10  # never shortened
        await backend.add_tags("a", ["t:{1}"], None)
        assert await redis.ttl("t:{1}") == -1  # an entry without expiry keeps it forever
        await backend.add_tags("a", ["t:{1}"], 10)
        assert await redis.ttl("t:{1}") == -1

    @pytest.mark.parametrize("decode_responses", [False, True])
    async def test_invalidations_tell_the_keys_they_deleted(self, decode_responses: bool) -> None:
        memory = InMemoryBackend()
        redis = RedisBackend(fakeredis.FakeAsyncRedis(decode_responses=decode_responses))
        for backend in (memory, redis):
            for key in ("a", "b"):
                await backend.set(key, b"x", 60)
                await backend.add_tags(key, ["t:{1}"], 60)
            deleted, keys = await backend.invalidate_tags_with_keys(["t:{1}", "t:{2}"])
            assert (deleted, sorted(keys or [])) == (2, ["a", "b"])
            assert await backend.invalidate_tags_with_keys(["t:{1}"]) == (0, [])

    async def test_backends_that_cant_tell_the_keys(self) -> None:
        class Untold(InMemoryBackend):
            invalidate_tags_with_keys = Backend.invalidate_tags_with_keys

            async def invalidate_tags(self, tags: Sequence[str]) -> int:
                return 3

        assert await Untold().invalidate_tags_with_keys(["t"]) == (3, None)


@pytest.mark.parametrize("backend_kind", ["memory", "redis"])
async def test_clearing_the_cache_keeps_locks_limits_and_revocations(backend_kind: str) -> None:
    backend: Backend = (
        InMemoryBackend() if backend_kind == "memory" else RedisBackend(fakeredis.FakeAsyncRedis())
    )
    cache = Cache(backend, prefix="app")
    blocklist = TokenBlocklist(cache)
    limiter = FastAPIRateLimiter(times=1, minutes=1, cache=cache, name="login")
    request = Request({"type": "http", "headers": [], "client": ("203.0.113.9", 1)})
    lock = cache.lock("job")
    assert await lock.acquire()
    await blocklist.revoke("jti", ttl=60)
    assert await limiter.hit(request) is None
    await cache.get_or_set("k", _one, expire=60)
    assert await cache.clear() == 1  # the cached value
    assert not await cache.lock("job").acquire()
    assert await blocklist.is_revoked("jti")
    assert await limiter.hit(request) is not None  # the counter is still there
    await lock.release()


async def _one() -> int:
    return 1


async def test_locks_release_only_with_their_token() -> None:
    redis = fakeredis.FakeAsyncRedis()
    backend = RedisBackend(redis)
    assert await backend.acquire_lock("lock", "mine", 5)
    assert not await backend.acquire_lock("lock", "theirs", 5)
    await backend.release_lock("lock", "theirs")
    assert await redis.get("lock") == b"mine"
    await backend.release_lock("lock", "mine")
    assert await redis.get("lock") is None
