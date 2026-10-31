"""FastAPITaskQueue: tasks on Redis streams, run at least once by the processes sharing them."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis
import pytest

from fastapi_alcazaba import BackendUnavailable, Cache, FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.backends.tiered import TieredBackend
from fastapi_alcazaba.background import FastAPITaskQueue, QueueFull
from fastapi_alcazaba.background.tasks import (
    ACK_SCRIPT,
    LEAVE_SCRIPT,
    READ_SCRIPT,
    RECLAIM_SCRIPT,
    _Entry,
)

SIGNING_KEY = "s" * 32
GROUP = "workers"
FAST = {"poll_interval": 0.02, "visibility_timeout": 0.5, "grace": 2.0}


async def eventually(check: Callable[[], Any], timeout: float = 3.0) -> None:
    """Wait until `check()` (or what it returns, when awaitable) is true."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def settled(*queues: FastAPITaskQueue) -> bool:
    """Nothing waiting, running or delayed in the queue, and no handler left in these processes."""
    for queue in queues:
        if await queue.pending() or queue._running:
            return False
    return True


class World:
    """A Redis shared by several processes: each cache gets a client of its own."""

    def __init__(self) -> None:
        self.server = fakeredis.FakeServer()
        self.raw: Any = fakeredis.FakeAsyncRedis(server=self.server)

    def backend(self) -> RedisBackend:
        return RedisBackend(fakeredis.FakeAsyncRedis(server=self.server))

    def cache(self, **options: Any) -> Cache:
        options.setdefault("signing_key", SIGNING_KEY)
        return Cache(self.backend(), prefix="app", **options)

    def queue(self, cache: Cache | None = None, **options: Any) -> FastAPITaskQueue:
        for name, value in FAST.items():
            options.setdefault(name, value)
        return FastAPITaskQueue(cache if cache is not None else self.cache(), **options)

    async def plant(self, queue: FastAPITaskQueue, fields: dict[str, bytes]) -> None:
        """Write an entry to the queue's stream by hand, as someone with access could."""
        queue._ready()
        await self.raw.xadd(queue._stream, fields)

    async def dead_worker_takes(self, queue: FastAPITaskQueue) -> list[Any]:
        """A worker that reads the waiting tasks and dies without finishing them."""
        queue._ready()
        with contextlib.suppress(Exception):  # BUSYGROUP: a worker created it already
            await self.raw.xgroup_create(queue._stream, GROUP, id="0", mkstream=True)
        rows: list[Any] = await self.raw.eval(
            READ_SCRIPT, 1, queue._stream, GROUP, "dead-worker", "10"
        )
        return rows

    async def dead_fields(self, queue: FastAPITaskQueue) -> list[dict[bytes, bytes]]:
        queue._ready()
        return [fields for _, fields in await self.raw.xrange(queue._dead)]


# redis-py marks the commands that a client may lack as abstract: fakeredis lacks a few.
class Swallowing(fakeredis.FakeAsyncRedis):  # pylint: disable=abstract-method
    """Its commands take a while and swallow cancellation, as fakeredis does on Python 3.11."""

    async def execute_command(self, *args: Any, **options: Any) -> Any:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(0.2)
        return await super().execute_command(*args, **options)  # type: ignore[no-untyped-call]


@pytest.fixture(name="world")
def fixture_world() -> World:
    return World()


def body(task: str, *args: Any, attempt: int = 0, **kwargs: Any) -> dict[str, Any]:
    return {
        "v": 2,
        "id": "t" * 32,
        "task": task,
        "args": list(args),
        "kwargs": kwargs,
        "attempt": attempt,
        "run": uuid.uuid4().hex,
        "until": time.time() + 3600,
    }


def sealed(queue: FastAPITaskQueue, payload: dict[str, Any]) -> bytes:
    queue._ready()
    return queue._seal(json.dumps(payload).encode())


class PaymentDeclined(Exception):
    pass


class TestConfiguration:
    @pytest.mark.parametrize(
        "options",
        [
            {"name": ""},
            {"concurrency": 0},
            {"grace": -1},
            {"poll_interval": 0},
            {"visibility_timeout": 0},
            {"max_deliveries": 0},
            {"max_payload_size": 0},
            {"max_length": 0},
            {"max_dead_letters": 0},
        ],
    )
    def test_invalid_settings(self, world: World, options: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="needs a name|must be|can't be"):
            FastAPITaskQueue(world.cache(), **options)

    @pytest.mark.parametrize(
        "backend", [InMemoryBackend(), TieredBackend(InMemoryBackend(), invalidation_channel=None)]
    )
    async def test_backends_without_streams(self, backend: Any) -> None:
        queue = FastAPITaskQueue(Cache(backend))

        @queue.task(name="job")
        async def job() -> None: ...

        with pytest.raises(NotImplementedError, match="needs Redis streams"):
            await queue.enqueue(job)
        with pytest.raises(NotImplementedError, match="needs Redis streams"):
            await queue.start()

    async def test_a_tiered_backend_in_front_of_redis(self, world: World) -> None:
        tiered = TieredBackend(world.backend(), invalidation_channel=None)
        queue = world.queue(Cache(tiered, prefix="app", signing_key=SIGNING_KEY))
        done = asyncio.Event()

        @queue.task(name="job")
        async def job() -> None:
            done.set()

        async with queue:
            await queue.enqueue(job)
            await asyncio.wait_for(done.wait(), 3)

    async def test_fastapicache_by_default(self, world: World) -> None:
        FastAPICache.init(world.backend(), prefix="app", signing_key=SIGNING_KEY)
        queue = FastAPITaskQueue(poll_interval=0.02)
        done = asyncio.Event()

        @queue.task(name="job")
        async def job() -> None:
            done.set()

        await queue.enqueue(job)
        assert await queue.pending() == 1
        async with queue:
            await asyncio.wait_for(done.wait(), 3)

    def test_each_lifespan_gets_the_cache_it_sets_up(self) -> None:
        """As in an app's tests: each TestClient runs the lifespan in an event loop of its own,
        and the lifespan sets FastAPICache up again."""
        queue = FastAPITaskQueue(poll_interval=0.02)
        ran: list[int] = []

        @queue.task(name="job")
        async def job(n: int) -> None:
            ran.append(n)

        async def lifespan(n: int) -> None:
            world = World()
            FastAPICache.init(world.backend(), prefix="app", signing_key=SIGNING_KEY)
            try:
                async with queue:
                    await asyncio.sleep(0.05)  # the workers wait for tasks
                    await queue.enqueue(job, n)
                    await eventually(lambda: n in ran)
                    assert await world.raw.exists(queue._stream)  # in this lifespan's Redis
            finally:
                FastAPICache.reset()

        for n in (1, 2):
            asyncio.run(lifespan(n))
        assert ran == [1, 2]

    async def test_keys_are_state_keys_in_one_hash_slot(self, world: World) -> None:
        cache = world.cache()
        queue, other = world.queue(cache), world.queue(cache, name="emails")
        queue._ready()
        other._ready()
        digest = hashlib.sha256(b"default").hexdigest()[:32]
        base = cache.state_key("tasks", f"{{{digest}}}")
        assert (queue._stream, queue._delayed, queue._dead) == (
            f"{base}:stream",
            f"{base}:delayed",
            f"{base}:dead",
        )
        assert other._stream != queue._stream

        @queue.task(name="job")
        async def job() -> None: ...

        await queue.enqueue(job)
        await queue.enqueue(job, delay=60)
        await cache.clear()  # cached responses only
        assert await queue.pending() == 2
        assert await other.pending() == 0

    async def test_a_warning_when_tasks_arent_signed(
        self,
        world: World,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async with world.queue(world.cache(signing_key=None)):
            pass
        assert "aren't signed" in caplog.text
        caplog.clear()
        async with world.queue():
            pass
        assert "aren't signed" not in caplog.text


class TestRegistration:
    async def test_names(self, world: World) -> None:
        queue = world.queue()

        @queue.task()
        async def default() -> None: ...

        @queue.task("custom")
        async def named() -> None: ...

        assert set(queue._tasks) == {f"{__name__}.{default.__qualname__}", "custom"}
        await queue.enqueue(default)
        await queue.enqueue("custom")
        assert await queue.pending() == 2

    def test_two_tasks_cant_have_the_same_name(self, world: World) -> None:
        queue = world.queue()

        @queue.task("job")
        async def job() -> None: ...

        with pytest.raises(ValueError, match="already a task named 'job'"):
            queue.task("job")(job)

    @pytest.mark.parametrize("options", [{"retries": -1}, {"backoff": -1.0}, {"max_backoff": -1.0}])
    def test_invalid_retry_settings(self, world: World, options: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="can't be negative"):
            world.queue().task(**options)

    async def test_only_registered_tasks_are_enqueued(self, world: World) -> None:
        queue = world.queue()

        async def stray() -> None: ...

        with pytest.raises(ValueError, match="isn't a task of the queue 'default'"):
            await queue.enqueue(stray)
        with pytest.raises(ValueError, match="isn't a task of the queue 'default'"):
            await queue.enqueue("stray")

    async def test_arguments_are_checked_when_enqueued(self, world: World) -> None:
        queue = world.queue(max_payload_size=100)

        @queue.task("job")
        # pylint: disable-next=unused-argument  # the task's signature, which enqueue() checks
        async def job(value: Any, *, flag: bool = False) -> None: ...

        with pytest.raises(TypeError, match="job: missing a required argument"):
            await queue.enqueue(job)
        with pytest.raises(TypeError, match="job: got an unexpected keyword argument 'other'"):
            await queue.enqueue(job, 1, other=2)
        with pytest.raises(TypeError, match="must be JSON-serialisable.*datetime"):
            await queue.enqueue(job, datetime.now(UTC))
        with pytest.raises(TypeError, match="must be JSON-serialisable"):
            await queue.enqueue(job, float("nan"))
        with pytest.raises(ValueError, match="more than max_payload_size \\(100\\)"):
            await queue.enqueue(job, "x" * 100)
        with pytest.raises(ValueError, match="delay can't be negative"):
            await queue.enqueue(job, 1, delay=-1)
        assert await queue.pending() == 0


class TestRunning:
    async def test_a_task_runs_with_its_arguments(self, world: World) -> None:
        queue = world.queue()
        received: list[Any] = []

        @queue.task("job")
        async def job(numbers: list[int], *, label: str, nested: dict[str, Any]) -> None:
            received.append((numbers, label, nested))

        async with queue:
            task_id = await queue.enqueue(job, (1, 2), label="x", nested={"a": None})
            await eventually(lambda: received)
            await eventually(lambda: settled(queue))
        assert len(task_id) == 32
        # As JSON gives them back: tuples are lists.
        assert received == [([1, 2], "x", {"a": None})]
        assert await world.raw.xlen(queue._stream) == 0  # finished tasks leave the stream

    async def test_sync_tasks_run_in_a_thread(self, world: World) -> None:
        queue = world.queue()
        threads: list[int] = []

        @queue.task("job")
        def job() -> None:
            threads.append(threading.get_ident())

        async with queue:
            await queue.enqueue(job)
            await eventually(lambda: threads)
        assert threads != [threading.get_ident()]

    async def test_concurrency(self, world: World) -> None:
        queue = world.queue(concurrency=5)
        running, most, done = [0], [0], [0]

        @queue.task("job")
        async def job() -> None:
            running[0] += 1
            most[0] = max(most[0], running[0])
            await asyncio.sleep(0.05)
            running[0] -= 1
            done[0] += 1

        for _ in range(6):
            await queue.enqueue(job)
        await queue.start(concurrency=2)
        await eventually(lambda: done[0] == 6)
        await queue.stop()
        assert most[0] == 2

    @pytest.mark.parametrize("delay", [0.3, timedelta(seconds=0.3)])
    async def test_delayed_tasks(self, world: World, delay: float | timedelta) -> None:
        queue = world.queue()
        ran: list[float] = []

        @queue.task("job")
        async def job() -> None:
            ran.append(time.monotonic())

        async with queue:
            start = time.monotonic()
            await queue.enqueue(job, delay=delay)
            assert await queue.pending() == 1
            await asyncio.sleep(0.15)
            assert not ran
            await eventually(lambda: ran)
        assert ran[0] - start >= 0.3

    async def test_enqueuing_wakes_the_workers_of_this_process(self, world: World) -> None:
        queue = world.queue(poll_interval=5)
        done = asyncio.Event()

        @queue.task("job")
        async def job() -> None:
            done.set()

        async with queue:
            await asyncio.sleep(0.05)  # the workers have looked, and wait for 5 s
            await queue.enqueue(job)
            await asyncio.wait_for(done.wait(), 1)

    async def test_each_task_runs_once_across_processes(self, world: World) -> None:
        queues = [world.queue(), world.queue()]
        ran: list[int] = []

        async def job(number: int) -> None:
            await asyncio.sleep(0.01)
            ran.append(number)

        for queue in queues:
            queue.task("job")(job)
        for number in range(30):
            await queues[number % 2].enqueue("job", number)
        for queue in queues:
            await queue.start()
        await eventually(lambda: len(ran) == 30)
        await asyncio.sleep(0.1)
        for queue in queues:
            await queue.stop()
        assert sorted(ran) == list(range(30))

    async def test_starting_and_stopping(self, world: World) -> None:
        queue = world.queue()
        await queue.stop()  # not running: nothing to do
        with pytest.raises(ValueError, match="concurrency must be at least 1"):
            await queue.start(concurrency=0)
        await queue.start()
        with pytest.raises(RuntimeError, match="already running"):
            await queue.start()
        await queue.stop()
        states = []
        async with queue.lifespan(None):
            states.append(queue._started)
        states.append(queue._started)
        async with queue as running:
            states.append(queue._started)
        states.append(queue._started)
        assert running is queue
        assert states == [True, False, True, False]

    async def test_a_stopped_worker_leaves_the_group(self, world: World) -> None:
        queue = world.queue()

        async def consumers() -> list[bytes]:
            if not await world.raw.exists(queue._stream):
                return []
            return [
                entry["name"] for entry in await world.raw.xinfo_consumers(queue._stream, GROUP)
            ]

        async with queue:
            await eventually(consumers)
            assert await consumers() == [queue._consumer.encode()]
        assert await consumers() == []


class TestRetries:
    async def test_a_failing_task_is_retried(self, world: World) -> None:
        queue = world.queue()
        attempts: list[float] = []

        @queue.task("job", retries=3, backoff=0.2)
        async def job() -> None:
            attempts.append(time.monotonic())
            if len(attempts) < 3:
                raise ConnectionError("the service is down")

        async with queue:
            await queue.enqueue(job)
            await eventually(lambda: len(attempts) == 3)
            await eventually(lambda: settled(queue))
        assert await queue.dead_letters() == []
        # 0.2 s, then 0.4 s, each between half and all of it.
        assert attempts[1] - attempts[0] >= 0.1
        assert attempts[2] - attempts[1] >= 0.2

    async def test_after_the_last_retry_the_dead_letters(
        self,
        world: World,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        queue = world.queue()
        runs = [0]

        @queue.task("charge", retries=1, backoff=0.01)
        async def charge(card: str) -> None:
            runs[0] += 1
            raise PaymentDeclined(f"card {card} declined")

        async with queue:
            task_id = await queue.enqueue(charge, "4111-1111-1111-1111")
            await eventually(queue.dead_letters)
        assert runs == [2]
        (dead,) = await queue.dead_letters()
        assert (dead.reason, dead.task, dead.task_id, dead.attempts) == (
            "failed",
            "charge",
            task_id,
            2,
        )
        assert dead.error == f"{__name__}.PaymentDeclined"
        assert abs((datetime.now(UTC) - dead.failed_at).total_seconds()) < 5
        assert await queue.pending() == 0
        # The error's message, which holds the card, is neither stored nor logged by the queue;
        # nor are the arguments.
        stored = b"".join(b"".join(fields.values()) for fields in await world.dead_fields(queue))
        assert b"declined" not in stored
        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "4111" not in messages
        assert "PaymentDeclined" in messages

    async def test_no_retries(self, world: World) -> None:
        queue = world.queue()
        runs = [0]

        @queue.task("job", retries=0)
        def job() -> None:
            runs[0] += 1
            raise ValueError("no")

        async with queue:
            await queue.enqueue(job)
            await eventually(queue.dead_letters)
        assert runs == [1]
        assert [dead.error for dead in await queue.dead_letters()] == ["ValueError"]

    def test_backoff(self, world: World) -> None:
        queue = world.queue()

        @queue.task("job", backoff=1.0, max_backoff=10.0)
        async def job() -> None: ...

        task = queue._tasks["job"]
        for _ in range(20):
            assert 0.5 <= task.delay(1) <= 1.0
            assert 2.0 <= task.delay(3) <= 4.0
            assert 5.0 <= task.delay(5) <= 10.0
            assert 5.0 <= task.delay(10_000) <= 10.0


class TestRefusedTasks:
    async def run(self, queue: FastAPITaskQueue) -> None:
        async with queue:
            await eventually(lambda: settled(queue))
            await eventually(lambda: not queue._running)

    async def test_an_unknown_task(self, world: World, caplog: pytest.LogCaptureFixture) -> None:
        queue = world.queue()
        await world.plant(queue, {"p": sealed(queue, body("os.system", "rm -rf /"))})
        await self.run(queue)
        (dead,) = await queue.dead_letters()
        assert (dead.reason, dead.task, dead.error) == ("unknown_task", "os.system", None)
        assert "Refused a task ('os.system')" in caplog.text
        assert "rm -rf" not in caplog.text

    @pytest.mark.parametrize("payload", [b"", json.dumps(body("job")).encode()])
    async def test_unsigned_tasks_in_a_signed_queue(self, world: World, payload: bytes) -> None:
        queue = world.queue()
        ran = []

        @queue.task("job")
        async def job() -> None:
            ran.append(1)

        await world.plant(queue, {"p": payload})
        await self.run(queue)
        assert not ran
        (dead,) = await queue.dead_letters()
        assert (dead.reason, dead.task) == ("tampered", None)

    async def test_altered_tasks(self, world: World) -> None:
        queue = world.queue()
        ran = []

        @queue.task("job")
        async def job(amount: int) -> None:
            ran.append(amount)

        good = sealed(queue, body("job", 10))
        altered = good.replace(b"10", b"99")
        await world.plant(queue, {"p": altered})
        await self.run(queue)
        assert not ran
        assert [dead.reason for dead in await queue.dead_letters()] == ["tampered"]

    async def test_a_task_sealed_for_another_queue(self, world: World) -> None:
        cache = world.cache()
        queue, other = world.queue(cache), world.queue(cache, name="other")
        ran = []

        @queue.task("job")
        async def job() -> None:
            ran.append(1)

        await world.plant(queue, {"p": sealed(other, body("job"))})
        await self.run(queue)
        assert not ran
        assert [dead.reason for dead in await queue.dead_letters()] == ["tampered"]

    @pytest.mark.parametrize("keys", [{}, {"signing_key": None, "encryption_key": "e" * 32}])
    async def test_copies_of_a_task_are_refused(
        self,
        world: World,
        keys: dict[str, Any],
    ) -> None:
        """Whoever can write to the backend, without the key, can't run a captured task again."""
        queue = world.queue(world.cache(**keys))
        ran = []

        @queue.task("refund")
        async def refund(order_id: int) -> None:
            ran.append(order_id)

        captured = sealed(queue, body("refund", 42))
        await world.plant(queue, {"p": captured})
        await self.run(queue)
        for _ in range(3):
            await world.plant(queue, {"p": captured})
        await self.run(queue)
        assert ran == [42]
        assert [dead.reason for dead in await queue.dead_letters()] == ["replayed"] * 3

    async def test_stale_tasks_are_refused(self, world: World) -> None:
        queue = world.queue()
        ran = []

        @queue.task("job")
        async def job() -> None:
            ran.append(1)

        await world.plant(queue, {"p": sealed(queue, {**body("job"), "until": time.time() - 1})})
        await self.run(queue)
        assert not ran
        (dead,) = await queue.dead_letters()
        assert dead.reason == "stale"
        assert await queue.requeue_dead_letters() == 1  # with a new deadline
        await self.run(queue)
        assert ran == [1]

    async def test_encrypted_tasks(self, world: World) -> None:
        queue = world.queue(world.cache(signing_key=None, encryption_key="e" * 32))
        ran = []

        @queue.task("job")
        async def job(secret: str) -> None:
            ran.append(secret)

        await queue.enqueue(job, "the-secret-argument")
        await queue.enqueue(job, "another-secret-argument", delay=0.05)
        stored = await world.raw.xrange(queue._stream)
        stored += await world.raw.zrange(queue._delayed, 0, -1)
        assert b"secret-argument" not in repr(stored).encode()
        async with queue:
            await eventually(lambda: len(ran) == 2)
        assert sorted(ran) == ["another-secret-argument", "the-secret-argument"]

    @pytest.mark.parametrize(
        "fields",
        [
            {"p": b"not json"},
            {"p": b"[" * 30_000 + b"]" * 30_000},
            {"p": b'{"v": 1}'},
            {"p": json.dumps({**body("job"), "attempt": True}).encode()},
            {"other": b"field"},
        ],
    )
    async def test_malformed_tasks(self, world: World, fields: dict[str, bytes]) -> None:
        queue = world.queue(world.cache(signing_key=None))

        @queue.task("job")
        async def job() -> None: ...

        await world.plant(queue, fields)
        await self.run(queue)
        assert [dead.reason for dead in await queue.dead_letters()] == ["malformed"]

    async def test_unsigned_tasks_in_an_unsigned_queue_run(self, world: World) -> None:
        queue = world.queue(world.cache(signing_key=None))
        ran: list[int] = []

        @queue.task("job")
        async def job(number: int) -> None:
            ran.append(number)

        await world.plant(queue, {"p": json.dumps(body("job", 5)).encode()})
        await self.run(queue)
        assert ran == [5]

    async def test_tasks_that_are_too_large(self, world: World) -> None:
        queue = world.queue(world.cache(signing_key=None), max_payload_size=100)
        await world.plant(queue, {"p": json.dumps(body("job", "x" * 300)).encode()})
        await self.run(queue)
        assert [dead.reason for dead in await queue.dead_letters()] == ["too_large"]
        (fields,) = await world.dead_fields(queue)
        assert fields[b"p"] == b""  # not copied

    async def test_arguments_that_dont_fit(self, world: World) -> None:
        queue = world.queue()
        ran = []

        @queue.task("job")
        async def job(number: int) -> None:
            ran.append(number)

        await world.plant(queue, {"p": sealed(queue, body("job", 1, 2, attempt=1))})
        await self.run(queue)
        assert not ran
        (dead,) = await queue.dead_letters()
        assert (dead.reason, dead.task, dead.attempts) == ("invalid_arguments", "job", 1)


class TestWorkersThatDie:
    async def test_their_tasks_are_taken_over(
        self,
        world: World,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger="fastapi_alcazaba.background.tasks")
        queue = world.queue(visibility_timeout=0.2)
        ran: list[int] = []

        @queue.task("job")
        async def job(number: int) -> None:
            ran.append(number)

        await queue.enqueue(job, 1)
        assert len(await world.dead_worker_takes(queue)) == 1
        async with queue:
            await eventually(lambda: ran == [1])
            await eventually(lambda: settled(queue))
        assert "Took over 1 tasks" in caplog.text

    async def test_long_tasks_arent_taken_over_while_their_worker_lives(self, world: World) -> None:
        queues = [world.queue(visibility_timeout=0.2), world.queue(visibility_timeout=0.2)]
        runs = [0]

        async def slow() -> None:
            runs[0] += 1
            await asyncio.sleep(0.8)

        for queue in queues:
            queue.task("slow")(slow)
            await queue.start()
        await queues[0].enqueue("slow")
        await eventually(lambda: settled(*queues), timeout=5)
        await asyncio.sleep(0.3)
        for queue in queues:
            await queue.stop()
        assert runs == [1]

    async def test_a_task_that_keeps_killing_its_workers(self, world: World) -> None:
        queue = world.queue(visibility_timeout=0.1, max_deliveries=1)
        ran = []

        @queue.task("crash")
        async def crash() -> None:
            ran.append(1)

        await queue.enqueue(crash)
        await world.dead_worker_takes(queue)
        async with queue:
            await eventually(queue.dead_letters)
        assert not ran
        (dead,) = await queue.dead_letters()
        assert (dead.reason, dead.task) == ("abandoned", "crash")

    async def test_a_task_deleted_while_its_worker_was_dead(self, world: World) -> None:
        queue = world.queue(visibility_timeout=0.1)

        @queue.task("job")
        async def job() -> None: ...

        await queue.enqueue(job)
        ((entry_id, _, _),) = await world.dead_worker_takes(queue)
        await world.raw.xdel(queue._stream, entry_id)
        async with queue:
            await asyncio.sleep(0.3)
        assert (await world.raw.xpending(queue._stream, GROUP))["pending"] == 0
        assert await queue.dead_letters() == []

    async def test_the_reclaim_script_drops_entries_that_are_gone(self, world: World) -> None:
        # Redis 6.2's XCLAIM answers nil for a deleted entry and keeps it pending.
        queue = world.queue()
        queue._ready()
        script = RECLAIM_SCRIPT.replace(
            "redis.call('XCLAIM', KEYS[1], ARGV[1], ARGV[2], ARGV[3], stale[1])", "{}"
        )
        await world.raw.xadd(queue._stream, {"p": b"x"})
        await world.dead_worker_takes(queue)
        await asyncio.sleep(0.01)
        assert await world.raw.eval(script, 1, queue._stream, GROUP, "me", "1", "10") == []
        assert (await world.raw.xpending(queue._stream, GROUP))["pending"] == 0

    async def test_a_task_taken_over_while_it_runs_here_doesnt_start_twice(
        self,
        world: World,
    ) -> None:
        queue = world.queue()
        started, release, runs = asyncio.Event(), asyncio.Event(), [0]

        @queue.task("slow")
        async def slow() -> None:
            runs[0] += 1
            started.set()
            await release.wait()

        async with queue:
            await queue.enqueue(slow)
            await asyncio.wait_for(started.wait(), 3)
            (entry_id,) = list(queue._running)
            queue._spawn(_Entry(entry_id, b"", 2))
            assert len(queue._running) == 1
            release.set()
            await eventually(lambda: not queue._running)
        assert runs == [1]


class TestShutdown:
    async def test_stop_when_the_client_swallows_the_cancellation(self, world: World) -> None:
        backend = RedisBackend(Swallowing(server=world.server))
        queue = world.queue(Cache(backend, prefix="app", signing_key=SIGNING_KEY))
        await queue.start()
        await asyncio.sleep(0.1)
        await asyncio.wait_for(queue.stop(), 3)  # the worker was cancelled in a command
        assert not queue._started

    async def test_stop_waits_for_the_running_tasks(self, world: World) -> None:
        queue = world.queue(grace=2)
        started, done = asyncio.Event(), asyncio.Event()

        @queue.task("job")
        async def job() -> None:
            started.set()
            await asyncio.sleep(0.2)
            done.set()

        await queue.start()
        await queue.enqueue(job)
        await asyncio.wait_for(started.wait(), 3)
        await queue.stop()
        assert done.is_set()
        assert await queue.pending() == 0

    async def test_tasks_still_running_after_the_grace_go_back_to_the_queue(
        self,
        world: World,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        first = world.queue(grace=0.1, visibility_timeout=30)
        second = world.queue(visibility_timeout=30)
        started, finished = asyncio.Event(), []

        async def job(number: int) -> None:
            if not started.is_set():
                started.set()
                await asyncio.sleep(30)
            finished.append(number)

        for queue in (first, second):
            queue.task("job")(job)
        await first.start()
        await first.enqueue("job", 7)
        await asyncio.wait_for(started.wait(), 3)
        await first.stop()
        assert "Cancelling 1 tasks of the queue 'default'" in caplog.text
        assert await first.pending() == 1
        # Taken by another process at once, not after the visibility timeout.
        async with second:
            await eventually(lambda: finished == [7])

    async def test_stop_doesnt_wait_for_a_stuck_backend_beyond_the_grace(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queue = world.queue(world.cache(backend_timeout=None), grace=0.2)
        queue._ready()
        reading = asyncio.Event()
        original = queue._client.eval

        async def stand_in(script: str, *args: Any) -> Any:
            if script == READ_SCRIPT:
                reading.set()
                await asyncio.sleep(30)
            return await original(script, *args)

        monkeypatch.setattr(queue._client, "eval", stand_in)
        await queue.start()
        await asyncio.wait_for(reading.wait(), 3)
        start = time.monotonic()
        await queue.stop()
        assert time.monotonic() - start < 2

    async def test_stop_when_the_backend_is_down(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queue = world.queue()
        await queue.start()
        original = queue._client.eval

        async def stand_in(script: str, *args: Any) -> Any:
            if script == LEAVE_SCRIPT:
                raise ConnectionError("down")
            return await original(script, *args)

        monkeypatch.setattr(queue._client, "eval", stand_in)
        await queue.stop()
        assert not queue._started


class TestBackendFailures:
    async def test_the_workers_recover(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        queue = world.queue(world.cache(circuit_breaker_failures=None))
        queue._ready()
        failures = [3]
        original = queue._client.eval

        async def stand_in(script: str, *args: Any) -> Any:
            if script == READ_SCRIPT and failures[0] > 0:
                failures[0] -= 1
                raise ConnectionError("down")
            return await original(script, *args)

        monkeypatch.setattr(queue._client, "eval", stand_in)
        done = asyncio.Event()

        @queue.task("job")
        async def job() -> None:
            done.set()

        async with queue:
            await queue.enqueue(job)
            await asyncio.wait_for(done.wait(), 5)
        assert "failed to reach the backend" in caplog.text

    async def test_the_group_comes_back_when_the_stream_is_deleted(self, world: World) -> None:
        queue = world.queue()
        ran: list[int] = []

        @queue.task("job")
        async def job(number: int) -> None:
            ran.append(number)

        async with queue:
            await queue.enqueue(job, 1)
            await eventually(lambda: ran == [1])
            await eventually(lambda: not queue._running)
            await world.raw.delete(queue._stream)  # the group goes with it
            await queue.enqueue(job, 2)
            await eventually(lambda: ran == [1, 2])

    async def test_a_key_of_another_type_in_the_way(
        self,
        world: World,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        queue = world.queue()
        queue._ready()
        await world.raw.set(queue._stream, b"x")
        done = asyncio.Event()

        @queue.task("job")
        async def job() -> None:
            done.set()

        async with queue:
            await eventually(lambda: "WRONGTYPE" in caplog.text)
            await world.raw.delete(queue._stream)
            await queue.enqueue(job)
            await asyncio.wait_for(done.wait(), 3)

    async def test_a_task_that_couldnt_be_settled_runs_again(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        queue = world.queue(visibility_timeout=0.2)
        queue._ready()
        failures = [1]
        original = queue._client.eval

        async def stand_in(script: str, *args: Any) -> Any:
            if script == ACK_SCRIPT and failures[0] > 0:
                failures[0] -= 1
                raise ConnectionError("down")
            return await original(script, *args)

        monkeypatch.setattr(queue._client, "eval", stand_in)
        runs = [0]

        @queue.task("job")
        async def job() -> None:
            runs[0] += 1

        async with queue:
            await queue.enqueue(job)
            await eventually(lambda: runs[0] == 2)
            await eventually(lambda: settled(queue))
        assert "Couldn't settle a task" in caplog.text

    async def test_enqueue_when_the_backend_is_down(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queue = world.queue()

        @queue.task("job")
        async def job() -> None: ...

        queue._ready()

        async def stand_in(*args: Any) -> Any:
            raise ConnectionError("down")

        monkeypatch.setattr(queue._client, "eval", stand_in)
        with pytest.raises(BackendUnavailable):
            await queue.enqueue(job)


class TestDelayedTasks:
    async def test_each_runs_once_across_processes(self, world: World) -> None:
        queues = [world.queue(), world.queue()]
        ran: list[int] = []

        async def job(number: int) -> None:
            ran.append(number)

        for queue in queues:
            queue.task("job")(job)
            await queue.start()
        for number in range(20):
            await queues[0].enqueue("job", number, delay=0.1)
        await eventually(lambda: len(ran) == 20)
        await asyncio.sleep(0.1)
        for queue in queues:
            await queue.stop()
        assert sorted(ran) == list(range(20))

    async def test_more_than_a_batch_due_at_once(self, world: World) -> None:
        queue = world.queue(poll_interval=3)
        ran = [0]

        @queue.task("job")
        async def job() -> None:
            ran[0] += 1

        for _ in range(150):
            await queue.enqueue(job, delay=0.01)
        await asyncio.sleep(0.02)
        async with queue:
            # Without waiting a poll interval between batches.
            await eventually(lambda: ran[0] == 150, timeout=2.5)


class TestQueueFull:
    async def test_enqueue_refuses_beyond_max_length(self, world: World) -> None:
        queue = world.queue(max_length=2)

        @queue.task("job")
        async def job() -> None: ...

        await queue.enqueue(job)
        await queue.enqueue(job, delay=60)
        with pytest.raises(QueueFull, match="already holds 2 tasks"):
            await queue.enqueue(job)
        assert await queue.pending() == 2


class TestDeadLetters:
    async def bury(self, queue: FastAPITaskQueue, count: int) -> None:
        """`count` tasks of `job` that failed for good."""
        for number in range(count):
            await queue.enqueue("job", number)
        async with queue:
            await eventually(lambda: settled(queue))
            await eventually(lambda: not queue._running)

    async def test_listing(self, world: World) -> None:
        queue = world.queue()

        @queue.task("job", retries=0)
        async def job(number: int) -> None:
            await asyncio.sleep(number / 50)
            raise ValueError("no")

        await self.bury(queue, 3)
        letters = await queue.dead_letters()
        assert len(letters) == 3
        assert [letter.failed_at for letter in letters] == sorted(
            (letter.failed_at for letter in letters), reverse=True
        )
        assert len(await queue.dead_letters(count=2)) == 2

    async def test_requeue_all(self, world: World) -> None:
        queue = world.queue()
        broken, ran = [True], []

        @queue.task("job", retries=1, backoff=0.01)
        async def job(number: int) -> None:
            if broken[0]:
                raise ValueError("no")
            ran.append(number)

        await self.bury(queue, 2)
        assert len(await queue.dead_letters()) == 2
        broken[0] = False
        async with queue:
            assert await queue.requeue_dead_letters() == 2
            await eventually(lambda: len(ran) == 2)
        assert await queue.dead_letters() == []

    async def test_requeued_tasks_get_their_retries_back(self, world: World) -> None:
        queue = world.queue()
        runs = [0]

        @queue.task("job", retries=1, backoff=0.01)
        # pylint: disable-next=unused-argument  # the task's signature
        async def job(number: int) -> None:
            runs[0] += 1
            raise ValueError("no")

        await self.bury(queue, 1)
        assert runs == [2]
        assert await queue.requeue_dead_letters() == 1
        await self.bury(queue, 0)
        assert runs == [4]
        assert [dead.attempts for dead in await queue.dead_letters()] == [2]

    async def test_requeue_some(self, world: World) -> None:
        queue = world.queue()

        @queue.task("job", retries=0)
        async def job(number: int) -> None:
            raise ValueError("no")

        await self.bury(queue, 2)
        first, second = await queue.dead_letters()
        assert await queue.requeue_dead_letters([first.id, "1-0"]) == 1
        assert await queue.requeue_dead_letters([first.id]) == 0  # gone
        assert [dead.id for dead in await queue.dead_letters()] == [second.id]
        assert await queue.pending() == 1
        with pytest.raises(ValueError, match="isn't the ID of a dead letter"):
            await queue.requeue_dead_letters(["$"])

    async def test_those_that_cant_run_stay(
        self,
        world: World,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        queue = world.queue()
        await world.plant(queue, {"p": b"planted"})
        await world.plant(queue, {"p": sealed(queue, body("later", 1))})
        await self.bury(queue, 0)
        assert sorted(dead.reason for dead in await queue.dead_letters()) == [
            "tampered",
            "unknown_task",
        ]
        assert await queue.requeue_dead_letters() == 0
        assert "stays: its signature doesn't match" in caplog.text
        assert "stays: no task of this process has its name" in caplog.text
        assert len(await queue.dead_letters()) == 2

        # A task that a newer version of the app knows (a rolling deploy).
        ran: list[int] = []

        @queue.task("later")
        async def later(number: int) -> None:
            ran.append(number)

        assert await queue.requeue_dead_letters() == 1
        await self.bury(queue, 0)
        assert ran == [1]
        assert [dead.reason for dead in await queue.dead_letters()] == ["tampered"]

    async def test_requeue_when_the_queue_is_full(self, world: World) -> None:
        queue = world.queue(max_length=1)

        @queue.task("job", retries=0)
        async def job(number: int) -> None:
            raise ValueError("no")

        await self.bury(queue, 1)
        await queue.enqueue(job, 2, delay=60)
        with pytest.raises(QueueFull):
            await queue.requeue_dead_letters()
        assert len(await queue.dead_letters()) == 1

    async def test_requeue_pages_through_them(self, world: World) -> None:
        queue = world.queue()

        @queue.task("job")
        # pylint: disable-next=unused-argument  # the task's signature
        async def job(number: int) -> None: ...

        queue._ready()
        for number in range(150):
            await world.raw.xadd(
                queue._dead, {"p": sealed(queue, body("job", number)), "reason": b"failed"}
            )
        assert await queue.requeue_dead_letters() == 150
        assert await queue.pending() == 150

    async def test_discard(self, world: World) -> None:
        queue = world.queue()

        @queue.task("job", retries=0)
        async def job(number: int) -> None:
            raise ValueError("no")

        await self.bury(queue, 2)
        first, second = await queue.dead_letters()
        assert await queue.discard_dead_letters([]) == 0
        assert await queue.discard_dead_letters([first.id, "1-0"]) == 1
        assert [dead.id for dead in await queue.dead_letters()] == [second.id]
        with pytest.raises(ValueError, match="isn't the ID of a dead letter"):
            await queue.discard_dead_letters(["+"])

    async def test_dead_letters_written_by_someone_else(self, world: World) -> None:
        queue = world.queue()
        queue._ready()
        await world.raw.xadd(queue._dead, {"junk": b"1"})
        await world.raw.xadd(queue._dead, {"attempts": b"\xc2\xb2", "at": b"nan"})
        await world.raw.xadd(queue._dead, {"attempts": b"3", "at": b"1e300"})
        epoch = datetime.fromtimestamp(0, UTC)
        letters = await queue.dead_letters()
        assert [(dead.reason, dead.task, dead.attempts, dead.failed_at) for dead in letters] == [
            ("", None, 3, epoch),
            ("", None, 0, epoch),
            ("", None, 0, epoch),
        ]
