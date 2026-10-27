"""Locks shared by every process: Cache.lock and the backends' lock operations."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import contextlib
import logging
import time

import fakeredis
import pytest

from fastapi_alcazaba import BackendUnavailable, Cache, FastAPICache
from fastapi_alcazaba.backends import Backend
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.backends.tiered import TieredBackend
from fastapi_alcazaba.background import Lock, LockNotAcquired


@pytest.fixture(name="cache", params=["memory", "redis"])
def fixture_cache(request: pytest.FixtureRequest) -> Cache:
    backend: Backend = (
        InMemoryBackend() if request.param == "memory" else RedisBackend(fakeredis.FakeAsyncRedis())
    )
    return Cache(backend, prefix="app")


class TestLock:
    async def test_one_holder_at_a_time(self, cache: Cache) -> None:
        first, second = cache.lock("report"), cache.lock("report")
        assert await first.acquire()
        assert first.held
        assert not await second.acquire()
        assert not second.held
        await first.release()
        assert not held(first)
        assert await second.acquire()
        await second.release()

    async def test_names_are_independent(self, cache: Cache) -> None:
        async with cache.lock("a"), cache.lock("b") as b:
            assert b.held

    async def test_the_context_manager(self, cache: Cache) -> None:
        async with cache.lock("report") as lock:
            assert lock.held
            with pytest.raises(LockNotAcquired, match="'report' is held by someone else"):
                async with cache.lock("report"):
                    pass
        assert not held(lock)
        async with cache.lock("report"):
            pass

    async def test_waiting_for_it(self, cache: Cache) -> None:
        first = cache.lock("report")
        await first.acquire()
        waiting = asyncio.create_task(cache.lock("report", wait=5).acquire())
        await asyncio.sleep(0.1)
        assert not waiting.done()
        await first.release()
        assert await asyncio.wait_for(waiting, 1)

    async def test_waiting_as_long_as_it_takes(self, cache: Cache) -> None:
        first = cache.lock("report")
        await first.acquire()
        asyncio.get_running_loop().call_later(0.2, lambda: asyncio.ensure_future(first.release()))
        assert await cache.lock("report", wait=None).acquire()

    async def test_giving_up(self, cache: Cache) -> None:
        await cache.lock("report").acquire()
        loop = asyncio.get_running_loop()
        start = loop.time()
        assert not await cache.lock("report", wait=0.2).acquire()
        assert 0.2 <= loop.time() - start < 0.5

    async def test_a_dead_holder_doesnt_keep_it(self, cache: Cache) -> None:
        dead = cache.lock("report", ttl=0.1, renew=False)
        await dead.acquire()
        await asyncio.sleep(0.15)
        assert not dead.held
        assert await cache.lock("report").acquire()

    async def test_renewals_keep_it(self, cache: Cache) -> None:
        async with cache.lock("report", ttl=0.1) as lock:
            await asyncio.sleep(0.35)
            assert lock.held
            assert not await cache.lock("report").acquire()

    async def test_only_the_holder_releases_it(self, cache: Cache) -> None:
        holder = cache.lock("report", ttl=0.1, renew=False)
        await holder.acquire()
        await asyncio.sleep(0.15)
        successor = cache.lock("report")
        assert await successor.acquire()
        await holder.release()  # its token is no longer the lock's
        assert not await cache.lock("report").acquire()

    async def test_the_name_is_hashed(self, cache: Cache) -> None:
        lock = cache.lock("user@example.com")
        assert lock.key.startswith("app!lock:{")
        assert "example" not in lock.key

    async def test_through_fastapi_cache(self) -> None:
        FastAPICache.init(InMemoryBackend())
        try:
            async with FastAPICache.lock("report", ttl=5) as lock:
                assert lock.ttl == 5
        finally:
            FastAPICache.reset()


class TestLostLocks:
    async def test_taken_away(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = InMemoryBackend()
        lock = Cache(backend).lock("report", ttl=0.1)
        await lock.acquire()
        backend._locks.clear()  # e.g. it expired during a long pause
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.background.locks"):
            await asyncio.sleep(0.1)
        assert not lock.held
        assert "Lost the lock 'report'" in caplog.text
        await lock.release()

    async def test_a_backend_that_comes_back_in_time(self) -> None:
        backend = Flaky()
        lock = Cache(backend, circuit_breaker_failures=None).lock("report", ttl=0.3)
        await lock.acquire()
        backend.failing = True
        await asyncio.sleep(0.15)  # one renewal fails, the lock hasn't expired
        backend.failing = False
        await asyncio.sleep(0.25)
        assert lock.held
        await lock.release()

    async def test_a_backend_down_until_it_expires(self) -> None:
        backend = Flaky()
        lock = Cache(backend, circuit_breaker_failures=None).lock("report", ttl=0.15)
        await lock.acquire()
        backend.failing = True
        await asyncio.sleep(0.3)
        assert not lock.held
        await lock.release()  # logged, not raised


class TestErrors:
    async def test_a_failing_backend(self) -> None:
        backend = Flaky()
        backend.failing = True
        with pytest.raises(BackendUnavailable, match="lock failed"):
            await Cache(backend).lock("report").acquire()

    async def test_acquiring_twice(self, cache: Cache) -> None:
        lock = cache.lock("report")
        await lock.acquire()
        with pytest.raises(RuntimeError, match="already held"):
            await lock.acquire()

    async def test_releasing_what_isnt_held(self, cache: Cache) -> None:
        await cache.lock("report").release()

    def test_validation(self, cache: Cache) -> None:
        with pytest.raises(ValueError, match="ttl"):
            cache.lock("report", ttl=0)
        with pytest.raises(ValueError, match="wait"):
            cache.lock("report", wait=-1)

    def test_backends_without_locks(self) -> None:
        class WithoutLocks(InMemoryBackend):
            supports_locks = False

        with pytest.raises(NotImplementedError, match="WithoutLocks can't hold locks"):
            Lock(Cache(WithoutLocks()), "report")

    @pytest.mark.parametrize("tiered", [False, True])
    async def test_backends_that_cant_renew_them(self, tiered: bool) -> None:
        """Written for 0.1, before extend_lock: the lock would lapse while still in use."""

        class Old(InMemoryBackend):
            extend_lock = Backend.extend_lock

        backend: Backend = TieredBackend(Old()) if tiered else Old()
        with pytest.raises(NotImplementedError, match="can't renew locks"):
            Lock(Cache(backend), "report")
        async with Lock(Cache(backend), "report", renew=False) as lock:
            assert lock.held

    async def test_releasing_keeps_the_callers_cancellation(self, cache: Cache) -> None:
        lock = cache.lock("report")
        await lock.acquire()
        releasing = asyncio.create_task(lock.release())
        await asyncio.sleep(0)  # waiting for the renewal task to stop
        releasing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await releasing
        assert await cache.lock("report").acquire()  # released all the same

    async def test_releasing_when_the_backend_swallows_the_cancellation(self) -> None:
        backend = SwallowingRenewals()
        lock = Cache(backend, prefix="app").lock("report", ttl=0.3)  # renewed every 0.1 s
        await lock.acquire()
        await asyncio.wait_for(backend.renewing.wait(), 1)
        await asyncio.wait_for(lock.release(), 1)  # not renewing forever meanwhile
        assert not lock.held
        assert await Cache(backend, prefix="app").lock("report").acquire()


class TestInMemoryLocks:
    async def test_held_locks_survive_the_eviction_of_entries(self) -> None:
        backend = InMemoryBackend(max_size=1)
        assert await backend.acquire_lock("lock", "mine", 5)
        await backend.set("a", b"1")
        await backend.set("b", b"2")
        assert not await backend.acquire_lock("lock", "theirs", 5)

    async def test_expired_locks_are_swept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = InMemoryBackend(max_size=2)
        for name in ("a", "b"):
            await backend.acquire_lock(name, "t", 1)
        later = time.monotonic() + 2  # neither was released
        monkeypatch.setattr(time, "monotonic", lambda: later)
        assert await backend.acquire_lock("c", "t", 1)
        assert list(backend._locks) == ["c"]

    async def test_extend(self) -> None:
        backend = InMemoryBackend()
        await backend.acquire_lock("lock", "mine", 0.05)
        assert await backend.extend_lock("lock", "mine", 5)
        assert not await backend.extend_lock("lock", "theirs", 5)
        await asyncio.sleep(0.1)
        assert not await backend.acquire_lock("lock", "theirs", 5)  # extended


class TestRedisLocks:
    async def test_extend(self) -> None:
        redis = fakeredis.FakeAsyncRedis()
        backend = RedisBackend(redis)
        assert await backend.acquire_lock("lock", "mine", 0.5)
        assert await backend.extend_lock("lock", "mine", 60)
        assert 50_000 < await redis.pttl("lock") <= 60_000
        assert not await backend.extend_lock("lock", "theirs", 60)
        assert not await backend.extend_lock("missing", "mine", 60)


def held(lock: Lock) -> bool:
    """`lock.held`, which mypy would take as unchanged since the last assert."""
    return lock.held


class SwallowingRenewals(InMemoryBackend):
    """Its renewals take a while and swallow cancellation, as fakeredis does on Python 3.11."""

    def __init__(self) -> None:
        super().__init__()
        self.renewing = asyncio.Event()

    async def extend_lock(self, key: str, token: str, ttl: float) -> bool:
        self.renewing.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(0.05)
        return await super().extend_lock(key, token, ttl)


class Flaky(InMemoryBackend):
    failing = False

    async def acquire_lock(self, key: str, token: str, ttl: float) -> bool:
        self._check()
        return await super().acquire_lock(key, token, ttl)

    async def extend_lock(self, key: str, token: str, ttl: float) -> bool:
        self._check()
        return await super().extend_lock(key, token, ttl)

    async def release_lock(self, key: str, token: str) -> None:
        self._check()
        await super().release_lock(key, token)

    def _check(self) -> None:
        if self.failing:
            raise ConnectionError("down")
