"""FastAPITaskQueue against real servers, each enabled by its environment variable:

    REDIS_URL=redis://localhost:6379/15            VALKEY_URL=valkey://localhost:6380/15
    CLUSTER_URL=redis://localhost:16382            (a Valkey or Redis cluster)

The scripts, the consumer group and the reclaiming as Redis and Valkey run them, between clients
of their own as between processes. Every queue has a random name and prefix, and only its keys
are deleted, so it's safe on a shared database.
"""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from fastapi_alcazaba import Cache
from fastapi_alcazaba.backends import Backend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.backends.tiered import TieredBackend
from fastapi_alcazaba.backends.valkey import ValkeyBackend
from fastapi_alcazaba.background import FastAPITaskQueue
from fastapi_alcazaba.background.tasks import READ_SCRIPT

pytestmark = pytest.mark.integration

KINDS = ["redis", "valkey", "redis-cluster", "valkey-cluster", "tiered-redis", "tiered-valkey"]
ENV = {
    "redis": "REDIS_URL",
    "valkey": "VALKEY_URL",
    "redis-cluster": "CLUSTER_URL",
    "valkey-cluster": "CLUSTER_URL",
    "tiered-redis": "REDIS_URL",
    "tiered-valkey": "VALKEY_URL",
}
SIGNING_KEY = "s" * 32
GROUP = "workers"


async def eventually(condition: Callable[[], Any], timeout: float = 10.0) -> None:
    """Wait until `condition()` (awaited when it's a coroutine) is true."""
    deadline = time.monotonic() + timeout
    while True:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def settled(*queues: FastAPITaskQueue) -> bool:
    for queue in queues:
        if await queue.pending() or queue._running:
            return False
    return True


def _client(kind: str, url: str) -> Any:
    if kind == "redis":
        import redis.asyncio

        return redis.asyncio.from_url(url)
    if kind == "valkey":
        import valkey.asyncio

        return valkey.asyncio.from_url(url)  # type: ignore[no-untyped-call]
    if kind == "redis-cluster":
        from redis.asyncio.cluster import RedisCluster

        return RedisCluster.from_url(url)
    from valkey.asyncio.cluster import ValkeyCluster

    return ValkeyCluster.from_url(url)


class Processes:
    """Queues of the same name, each with a client of its own: processes sharing a server."""

    def __init__(self, kind: str, url: str) -> None:
        self.kind = kind
        self.url = url
        self.prefix = f"fastapi-alcazaba-test-{uuid.uuid4().hex[:8]}"
        self.name = uuid.uuid4().hex
        self.clients: list[Any] = []
        self.queues: list[FastAPITaskQueue] = []
        self.raw = self._new_client()

    def _new_client(self) -> Any:
        client = _client(self.kind.removeprefix("tiered-"), self.url)
        self.clients.append(client)
        return client

    def queue(self, **options: Any) -> FastAPITaskQueue:
        client = self._new_client()
        backend: Backend = ValkeyBackend(client) if "valkey" in self.kind else RedisBackend(client)
        if self.kind.startswith("tiered-"):
            backend = TieredBackend(backend, invalidation_channel=None)
        options.setdefault("poll_interval", 0.05)
        options.setdefault("visibility_timeout", 1.0)
        cache = Cache(backend, prefix=self.prefix, signing_key=SIGNING_KEY)
        queue = FastAPITaskQueue(cache, name=self.name, **options)
        queue._ready()
        self.queues.append(queue)
        return queue

    async def close(self) -> None:
        for queue in self.queues:
            await queue.stop()
        if self.queues:
            queue = self.queues[0]
            for key in (queue._stream, queue._delayed, queue._dead):
                await self.raw.delete(key)
        for client in self.clients:
            await client.aclose()


@pytest.fixture(name="processes", params=KINDS)
async def fixture_processes(request: pytest.FixtureRequest) -> AsyncIterator[Processes]:
    url = os.environ.get(ENV[request.param])
    if not url:
        pytest.skip(f"{ENV[request.param]} not set")
    world = Processes(request.param, url)
    yield world
    await world.close()


async def test_each_task_runs_once_across_processes(processes: Processes) -> None:
    queues = [processes.queue(), processes.queue()]
    ran: list[int] = []

    async def job(number: int) -> None:
        await asyncio.sleep(0.01)
        ran.append(number)

    for queue in queues:
        queue.task("job")(job)
    for number in range(20):
        await queues[number % 2].enqueue("job", number)
    for queue in queues:
        await queue.start()
    await eventually(lambda: settled(*queues))
    assert sorted(ran) == list(range(20))
    assert await processes.raw.xlen(queues[0]._stream) == 0


async def test_delayed_tasks_run_once(processes: Processes) -> None:
    queues = [processes.queue(), processes.queue()]
    ran: list[int] = []

    async def job(number: int) -> None:
        ran.append(number)

    for queue in queues:
        queue.task("job")(job)
        await queue.start()
    start = time.monotonic()
    for number in range(10):
        await queues[0].enqueue("job", number, delay=0.3)
    await eventually(lambda: len(ran) == 10)
    assert time.monotonic() - start >= 0.3
    await eventually(lambda: settled(*queues))
    assert sorted(ran) == list(range(10))


async def test_retries_dead_letters_and_requeue(processes: Processes) -> None:
    queue = processes.queue()
    broken, runs = [True], [0]

    @queue.task("job", retries=1, backoff=0.05)
    async def job() -> None:
        runs[0] += 1
        if broken[0]:
            raise ValueError("no")

    async with queue:
        task_id = await queue.enqueue(job)
        await eventually(queue.dead_letters)
        (dead,) = await queue.dead_letters()
        assert (dead.reason, dead.task_id, dead.error, dead.attempts) == (
            "failed",
            task_id,
            "ValueError",
            2,
        )
        broken[0] = False
        assert await queue.requeue_dead_letters() == 1
        await eventually(lambda: runs[0] == 3)
        await eventually(lambda: settled(queue))
    assert await queue.dead_letters() == []


async def test_tasks_of_a_dead_worker_are_taken_over(processes: Processes) -> None:
    queue = processes.queue(visibility_timeout=0.3)
    ran: list[int] = []

    @queue.task("job")
    async def job(number: int) -> None:
        ran.append(number)

    await queue.enqueue(job, 1)
    await processes.raw.xgroup_create(queue._stream, GROUP, id="0", mkstream=True)
    rows = await processes.raw.eval(READ_SCRIPT, 1, queue._stream, GROUP, "dead-worker", "10")
    assert len(rows) == 1  # read, and never finished
    async with queue:
        await eventually(lambda: ran == [1])
        await eventually(lambda: settled(queue))


async def test_long_tasks_are_kept_by_their_worker(processes: Processes) -> None:
    queues = [processes.queue(visibility_timeout=0.3), processes.queue(visibility_timeout=0.3)]
    runs = [0]

    async def slow() -> None:
        runs[0] += 1
        await asyncio.sleep(1.2)

    for queue in queues:
        queue.task("slow")(slow)
        await queue.start()
    await queues[0].enqueue("slow")
    await eventually(lambda: settled(*queues))
    await asyncio.sleep(0.5)
    assert runs == [1]


async def test_planted_tasks_are_refused(processes: Processes) -> None:
    queue = processes.queue()
    ran = []

    @queue.task("job")
    async def job() -> None:
        ran.append(1)

    planted = {"v": 1, "id": "x", "task": "job", "args": [], "kwargs": {}, "attempt": 0}
    await processes.raw.xadd(queue._stream, {"p": json.dumps(planted)})
    async with queue:
        await eventually(queue.dead_letters)
        await eventually(lambda: settled(queue))
    assert not ran
    assert [dead.reason for dead in await queue.dead_letters()] == ["tampered"]


async def test_queue_and_consumer_bookkeeping(processes: Processes) -> None:
    queue = processes.queue(max_length=2)

    @queue.task("job")
    async def job() -> None: ...

    await queue.enqueue(job)
    await queue.enqueue(job, delay=60)
    assert await queue.pending() == 2

    async def only_the_delayed_one_left() -> bool:
        return await queue.pending() == 1

    async with queue:
        await eventually(only_the_delayed_one_left)
        consumer = queue._consumer
    names = [entry["name"] for entry in await processes.raw.xinfo_consumers(queue._stream, GROUP)]
    assert consumer.encode() not in names  # it left the group when it stopped
