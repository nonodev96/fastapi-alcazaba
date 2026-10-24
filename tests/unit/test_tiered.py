import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import fakeredis
import pytest

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.backends.tiered import TieredBackend
from fastapi_alcazaba.decorator import cache


async def eventually(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
def redis() -> Any:
    return fakeredis.FakeAsyncRedis()


@pytest.fixture
async def processes(redis: Any) -> AsyncIterator[tuple[TieredBackend, TieredBackend]]:
    """Two processes: each with its own local level, sharing Redis and the channel."""
    first, second = TieredBackend(RedisBackend(redis)), TieredBackend(RedisBackend(redis))
    for process in (first, second):
        await process.get("warm-up")  # starts listening
    await eventually(lambda: first._subscribed and second._subscribed)
    yield first, second
    await first.close()
    await second.close()


class TestLocalLevel:
    async def test_hits_skip_the_shared_backend(
        self, processes: tuple[TieredBackend, TieredBackend], redis: Any
    ) -> None:
        first, _ = processes
        await redis.set("k", b"v", ex=100)
        assert (await first.get_with_ttl("k"))[1] == b"v"  # from Redis
        await redis.set("k", b"changed behind its back", ex=100)
        ttl, value = await first.get_with_ttl("k")  # from memory
        assert value == b"v"
        assert 95 < ttl <= 100  # the shared entry's remaining time, not local_ttl
        assert first.local_hits == 1

    async def test_own_writes_stay_local(
        self, processes: tuple[TieredBackend, TieredBackend]
    ) -> None:
        first, _ = processes
        await first.set("k", b"v", expire=60)
        await asyncio.sleep(0.05)  # its own announcement arrives and is ignored
        hits = first.local_hits
        assert await first.get("k") == b"v"
        assert first.local_hits == hits + 1

    async def test_never_outlives_the_shared_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tiered = TieredBackend(InMemoryBackend(), local_ttl=60)
        await tiered.set("k", b"v", expire=2)
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now + 3)
        assert await tiered._from_local("k") is None

    async def test_without_pub_sub_copies_last_local_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shared = InMemoryBackend()
        first = TieredBackend(shared, local_ttl=5)
        second = TieredBackend(shared, local_ttl=5)
        assert first.channel is None
        await first.set("k", b"old", expire=600)
        assert await second.get("k") == b"old"
        await first.set("k", b"new", expire=600)
        assert await second.get("k") == b"old"  # stale, but only for local_ttl
        start = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: start + 6)
        assert await second.get("k") == b"new"

    async def test_serves_local_copies_while_the_shared_backend_is_down(self) -> None:
        class Flaky(InMemoryBackend):
            down = False

            async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
                if self.down:
                    raise ConnectionError("down")
                return await super().get_with_ttl(key)

        shared = Flaky()
        tiered = TieredBackend(shared)
        await tiered.set("k", b"v", expire=60)
        shared.down = True
        assert await tiered.get("k") == b"v"

    def test_local_ttl_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="local_ttl"):
            TieredBackend(InMemoryBackend(), local_ttl=0)


class TestInvalidationChannel:
    async def test_writes_drop_the_other_processes_copies(
        self, processes: tuple[TieredBackend, TieredBackend]
    ) -> None:
        first, second = processes
        await first.set("k", b"old", expire=60)
        assert await second.get("k") == b"old"
        await first.set("k", b"new", expire=60)
        await eventually(lambda: "k" not in second.local._store)
        assert await second.get("k") == b"new"

    async def test_clears_reach_every_process(
        self, processes: tuple[TieredBackend, TieredBackend]
    ) -> None:
        first, second = processes
        for key in ("ns:1", "ns:2", "other:1"):
            await first.set(key, b"v", expire=60)
            await second.get(key)
        await first.clear(namespace="ns")
        await eventually(lambda: set(second.local._store) == {"other:1"})
        await first.clear(key="other:1")
        await eventually(lambda: not second.local._store)

    async def test_tag_invalidations_reach_every_process(
        self, processes: tuple[TieredBackend, TieredBackend]
    ) -> None:
        first, second = processes
        await first.set("product:1", b"v", expire=60)
        await first.add_tags("product:1", ["t:{p1}"], 60)
        await second.get("product:1")
        await second.local.add_tags("product:1", ["t:{p1}"], 60)
        assert await first.invalidate_tags(["t:{p1}"]) == 1
        await eventually(lambda: not second.local._store)
        assert await second.get("product:1") is None

    async def test_closing_drops_the_local_level(
        self, processes: tuple[TieredBackend, TieredBackend], redis: Any
    ) -> None:
        first, _ = processes
        await first.set("k", b"v", expire=60)
        await first.close()
        assert not first.local._store
        await redis.set("k", b"changed", ex=60)
        assert await first.get("k") == b"changed"

    async def test_a_lost_subscription_drops_the_local_level_until_it_is_back(
        self, processes: tuple[TieredBackend, TieredBackend]
    ) -> None:
        first, _ = processes
        await first.set("k", b"v", expire=60)
        assert first.local._store
        await first._pubsub.aclose()  # the connection of the subscription goes away
        await eventually(lambda: not first._subscribed)
        assert not first.local._store  # invalidations may have been missed
        await first.set("k", b"v", expire=60)
        assert not first.local._store  # not kept while unsubscribed
        await eventually(lambda: first._subscribed)  # reconnects by itself

    async def test_garbage_on_the_channel_is_ignored(
        self, processes: tuple[TieredBackend, TieredBackend], redis: Any
    ) -> None:
        first, second = processes
        for garbage in (b"not json", b"[1, 2]", b'{"tags": "x"}', b'{"key": 1}'):
            await redis.publish("fastapi_alcazaba:invalidations", garbage)
        await first.set("k", b"v", expire=60)
        await eventually(lambda: second._subscribed)
        assert await second.get("k") == b"v"
        assert second._listener is not None
        assert not second._listener.done()


async def test_capabilities_follow_the_shared_backend(redis: Any) -> None:
    tiered = TieredBackend(RedisBackend(redis))
    assert tiered.supports_locks
    assert tiered.supports_tags
    assert await tiered.acquire_lock("lock", "mine", 5)
    assert not await tiered.acquire_lock("lock", "theirs", 5)
    await tiered.release_lock("lock", "mine")
    assert not TieredBackend(InMemoryBackend()).supports_locks


async def test_through_the_decorator(processes: tuple[TieredBackend, TieredBackend]) -> None:
    first, _ = processes
    FastAPICache.init(first, encryption_key="e" * 32)
    calls: list[int] = []

    @cache(expire=60, tags=["catalog"])
    async def catalog() -> list[int]:
        calls.append(1)
        return [1, 2, 3]

    for _ in range(3):
        assert await catalog() == [1, 2, 3]
    assert calls == [1]
    assert first.local_hits >= 2
    assert await FastAPICache.invalidate_tags("catalog") == 1
    assert await catalog() == [1, 2, 3]
    assert calls == [1, 1]


async def test_close_does_not_hang_when_the_cancellation_gets_lost() -> None:
    """Python 3.11's asyncio.wait_for (used inside redis clients) can swallow a cancellation:
    close() must still return, and the listener stop once its connection is closed."""

    class StubbornPubSub:
        closed = False
        messages: list[dict[str, Any]] = []

        async def subscribe(self, channel: str) -> None:
            pass

        async def listen(self) -> AsyncIterator[dict[str, Any]]:
            while not self.closed:
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue  # what a buggy wait_for does
                while self.messages:
                    yield self.messages.pop()

        async def aclose(self) -> None:
            self.closed = True

    class StubbornClient:
        def pubsub(self) -> StubbornPubSub:
            return StubbornPubSub()

        async def publish(self, channel: str, message: str) -> None:
            pass

    class Remote(InMemoryBackend):
        _client = StubbornClient()

    tiered = TieredBackend(Remote())
    await tiered.get("warm-up")
    await eventually(lambda: tiered._subscribed)
    listener = tiered._listener
    start = time.monotonic()
    await tiered.close()
    assert time.monotonic() - start < 2.5
    assert listener is not None
    await eventually(listener.done)
