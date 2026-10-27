"""FastAPILeader: one leader among the processes, on a renewed lock."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import contextlib
import logging
from collections.abc import Callable

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_alcazaba import Cache, FastAPICache
from fastapi_alcazaba.backends import Backend
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.background import FastAPILeader


@pytest.fixture(name="cache", params=["memory", "redis"])
def fixture_cache(request: pytest.FixtureRequest) -> Cache:
    backend: Backend = (
        InMemoryBackend()
        if request.param == "memory"
        else RedisBackend(fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer()))
    )
    return Cache(backend, prefix="app")


class Flaky(InMemoryBackend):
    failing = False

    async def acquire_lock(self, key: str, token: str, ttl: float) -> bool:
        if self.failing:
            raise ConnectionError("down")
        return await super().acquire_lock(key, token, ttl)


class Swallowing(InMemoryBackend):
    """Its lock requests take a while and swallow cancellation, as fakeredis does on Python 3.11."""

    async def acquire_lock(self, key: str, token: str, ttl: float) -> bool:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(0.5)
        return await super().acquire_lock(key, token, ttl)


async def until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


class Worker:
    """A function to run while leading, recording its runs."""

    def __init__(self) -> None:
        self.started = 0
        self.cancelled = 0

    async def __call__(self) -> None:
        self.started += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def leader_with_worker(name: str, cache: Cache, ttl: float = 0.3) -> tuple[FastAPILeader, Worker]:
    leader, worker = FastAPILeader(name, ttl=ttl, cache=cache), Worker()

    async def work() -> None:
        await worker()

    leader.run(work)
    return leader, worker


class TestElection:
    async def test_one_leader_at_a_time(self, cache: Cache) -> None:
        first, first_worker = leader_with_worker("outbox", cache)
        second, second_worker = leader_with_worker("outbox", cache)
        async with first:
            await until(lambda: first.is_leader)
            async with second:
                await asyncio.sleep(0.3)  # campaigns a few times
                assert not second.is_leader
                assert (first_worker.started, second_worker.started) == (1, 0)
                await first.stop()
                assert not first.is_leader
                assert first_worker.cancelled == 1
                await until(lambda: second.is_leader)  # released: taken over at once
                await until(lambda: second_worker.started == 1)
            assert second_worker.cancelled == 1

    async def test_names_are_independent(self, cache: Cache) -> None:
        async with (
            FastAPILeader("a", ttl=0.3, cache=cache) as a,
            FastAPILeader("b", ttl=0.3, cache=cache) as b,
        ):
            await until(lambda: a.is_leader and b.is_leader)

    async def test_a_dead_leader_is_replaced(
        self,
        cache: Cache,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        dead = FastAPILeader("outbox", ttl=0.2, cache=cache)
        await dead.start()
        await until(lambda: dead.is_leader)
        campaign, lock = dead._campaign, dead._lock
        assert campaign is not None
        assert lock is not None
        assert lock._renewal is not None

        async def released_by_nobody() -> None:
            pass

        # The process dies: nobody renews the lock nor releases it.
        monkeypatch.setattr(lock, "release", released_by_nobody)
        lock._renewal.cancel()
        campaign.cancel()
        await asyncio.wait({campaign})
        successor = FastAPILeader("outbox", ttl=0.2, cache=cache)
        async with successor:
            await asyncio.sleep(0.05)
            assert not successor.is_leader  # not before the lock expires
            await until(lambda: successor.is_leader)

    async def test_losing_the_lock_cancels_and_campaigns_again(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        backend = InMemoryBackend()
        leader, worker = leader_with_worker("outbox", Cache(backend))
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.background.leader"):
            async with leader:
                await until(lambda: worker.started == 1)
                backend._locks.clear()  # e.g. it expired during a long pause
                await until(lambda: worker.cancelled == 1)
                await until(lambda: worker.started == 2)  # elected again
                assert leader.is_leader
        assert "Lost the leadership of 'outbox'" in caplog.text

    async def test_failing_functions_are_logged(
        self,
        cache: Cache,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        leader = FastAPILeader("outbox", ttl=0.3, cache=cache)
        ran: list[str] = []

        @leader.run
        async def fails() -> None:
            ran.append("fails")
            raise RuntimeError("boom")

        @leader.run
        async def returns() -> None:
            ran.append("returns")

        with caplog.at_level(logging.ERROR, "fastapi_alcazaba.background.leader"):
            async with leader:
                await until(lambda: len(ran) == 2)
                await asyncio.sleep(0.05)
                assert leader.is_leader  # still leading
        assert sorted(ran) == ["fails", "returns"]
        assert "fails failed while leading 'outbox'" in caplog.text

    async def test_without_functions(self, cache: Cache) -> None:
        async with FastAPILeader("outbox", ttl=0.3, cache=cache) as leader:
            await until(lambda: leader.is_leader)
        assert not leader.is_leader

    async def test_is_leader_from_a_thread(self, cache: Cache) -> None:
        async with FastAPILeader("outbox", ttl=0.3, cache=cache) as leader:
            assert not await asyncio.to_thread(lambda: leader.is_leader)
            await until(lambda: leader.is_leader)
            assert await asyncio.to_thread(lambda: leader.is_leader)

    async def test_through_fastapi_cache(self) -> None:
        FastAPICache.init(InMemoryBackend())
        async with FastAPILeader("outbox", ttl=0.3) as leader:
            await until(lambda: leader.is_leader)


class TestBackendFailures:
    async def test_nobody_leads_while_it_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = Flaky()
        backend.failing = True
        leader, worker = leader_with_worker("outbox", Cache(backend, circuit_breaker_failures=None))
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.background.leader"):
            async with leader:
                await asyncio.sleep(0.2)
                assert not leader.is_leader
                assert worker.started == 0
                backend.failing = False
                await until(lambda: leader.is_leader)
        assert "No election of 'outbox': the backend failed" in caplog.text

    async def test_an_open_circuit_is_only_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = Flaky()
        backend.failing = True
        leader = FastAPILeader("outbox", ttl=0.3, cache=Cache(backend, circuit_breaker_failures=1))
        with caplog.at_level(logging.DEBUG, "fastapi_alcazaba.background.leader"):
            async with leader:
                await until(lambda: "circuit breaker is open" in caplog.text)


class TestLifecycle:
    def test_lifespan(self) -> None:
        leader = FastAPILeader("outbox", ttl=0.3, cache=Cache(InMemoryBackend()))
        ran: list[bool] = []

        @leader.run
        async def work() -> None:
            ran.append(True)

        async def leads() -> bool:
            await until(lambda: bool(ran))
            return leader.is_leader

        app = FastAPI(lifespan=leader.lifespan)
        with TestClient(app) as client:
            assert client.portal is not None
            assert client.portal.call(leads)
        assert leader._campaign is None

    async def test_starting_twice(self, cache: Cache) -> None:
        async with FastAPILeader("outbox", cache=cache) as leader:
            with pytest.raises(RuntimeError, match="already running"):
                await leader.start()

    async def test_stopping_when_the_backend_swallows_the_cancellation(self) -> None:
        cache = Cache(Swallowing(), prefix="app")
        leader, _ = leader_with_worker("outbox", cache, ttl=3)  # longer than a lock request
        await leader.start()
        await asyncio.sleep(0.1)  # asking for the lock
        await asyncio.wait_for(leader.stop(), 1)
        assert not leader.is_leader
        other, worker = leader_with_worker("outbox", cache, ttl=3)
        async with other:  # the lock was given back
            await until(lambda: worker.started == 1)

    async def test_stopping_what_isnt_running(self, cache: Cache) -> None:
        await FastAPILeader("outbox", cache=cache).stop()

    async def test_functions_are_added_before_starting(self, cache: Cache) -> None:
        async with FastAPILeader("outbox", cache=cache) as leader:

            async def late() -> None:
                pass  # pragma: no cover

            with pytest.raises(RuntimeError, match="before the leader starts"):
                leader.run(late)


class TestValidation:
    def test_arguments(self) -> None:
        with pytest.raises(ValueError, match="name"):
            FastAPILeader("")
        with pytest.raises(ValueError, match="ttl"):
            FastAPILeader("outbox", ttl=0)
        with pytest.raises(ValueError, match="ttl"):
            FastAPILeader("outbox", ttl=float("inf"))

    def test_functions(self) -> None:
        leader = FastAPILeader("outbox")

        def sync() -> None:
            pass  # pragma: no cover

        # pylint: disable-next=unused-argument  # a function with arguments, which the leader refuses
        async def with_arguments(x: int) -> None:
            pass  # pragma: no cover

        with pytest.raises(TypeError, match="sync must be an async function"):
            leader.run(sync)  # type: ignore[type-var]
        with pytest.raises(TypeError, match="with_arguments must take no arguments"):
            leader.run(with_arguments)  # type: ignore[type-var]

    async def test_backends_without_locks(self) -> None:
        class WithoutLocks(InMemoryBackend):
            supports_locks = False

        with pytest.raises(NotImplementedError, match="can't hold locks"):
            await FastAPILeader("outbox", cache=Cache(WithoutLocks())).start()

    async def test_backends_that_cant_renew_locks(self) -> None:
        class Old(InMemoryBackend):
            extend_lock = Backend.extend_lock

        with pytest.raises(NotImplementedError, match="can't renew locks"):
            await FastAPILeader("outbox", cache=Cache(Old())).start()
