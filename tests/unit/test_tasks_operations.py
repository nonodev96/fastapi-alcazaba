"""FastAPITaskQueue's operations: timeouts, which errors retry, unique keys, status records and
cancelling."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import json
import threading
import time
import uuid
from typing import Any

import pytest

from fastapi_alcazaba.background import FastAPITaskQueue, QueueFull, TaskStatus
from fastapi_alcazaba.background import tasks as tasks_module
from fastapi_alcazaba.background.tasks import CLAIM_SCRIPT, STATES
from tests.unit.test_tasks import World, body, eventually, sealed, settled


@pytest.fixture(name="world")
def fixture_world() -> World:
    return World()


async def state(queue: FastAPITaskQueue, task_id: str) -> str | None:
    found = await queue.status(task_id)
    return found.state if found is not None else None


class TestTimeouts:
    async def test_a_hung_task_is_cut_and_retried(self, world: World) -> None:
        queue = world.queue(concurrency=1)
        runs: list[int] = []
        others: list[str] = []

        @queue.task("hangs", retries=1, backoff=0.01, timeout=0.1)
        async def hangs() -> None:
            runs.append(1)
            await asyncio.Event().wait()  # never answers

        @queue.task("other")
        async def other() -> None:
            others.append("ran")

        async with queue:
            task_id = await queue.enqueue(hangs)
            await queue.enqueue(other)
            await eventually(queue.dead_letters)
            await eventually(lambda: others == ["ran"])  # its one slot was freed
        assert len(runs) == 2
        (dead,) = await queue.dead_letters()
        assert (dead.reason, dead.error, dead.attempts) == ("failed", "TimeoutError", 2)
        found = await queue.status(task_id)
        assert found is not None
        assert (found.state, found.error, found.reason) == ("dead", "TimeoutError", "failed")

    async def test_a_sync_task_frees_its_slot(self, world: World) -> None:
        queue = world.queue(concurrency=1)
        release = threading.Event()
        others: list[str] = []

        @queue.task("slow", retries=0, timeout=0.1)
        def slow() -> None:
            release.wait(5)  # its thread can't be stopped: it runs on

        @queue.task("other")
        async def other() -> None:
            others.append("ran")

        try:
            async with queue:
                await queue.enqueue(slow)
                await queue.enqueue(other)
                await eventually(lambda: others == ["ran"])
                assert [dead.error for dead in await queue.dead_letters()] == ["TimeoutError"]
        finally:
            release.set()

    async def test_the_task_s_own_timeout_error_isnt_one(
        self,
        world: World,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        queue = world.queue()

        @queue.task("job", retries=0, timeout=10)
        async def job() -> None:
            raise TimeoutError("the service's own")

        async with queue:
            await queue.enqueue(job)
            await eventually(queue.dead_letters)
        assert "past its timeout" not in caplog.text

    async def test_fast_tasks_arent_affected(self, world: World) -> None:
        queue = world.queue()
        runs: list[int] = []

        @queue.task("job", timeout=5)
        async def job(n: int) -> None:
            runs.append(n)

        async with queue:
            task_id = await queue.enqueue(job, 1)
            await eventually(lambda: runs == [1])
            await eventually(lambda: settled(queue))
        assert await state(queue, task_id) == "succeeded"

    @pytest.mark.parametrize("timeout", [0, -1])
    def test_invalid(self, world: World, timeout: float) -> None:
        with pytest.raises(ValueError, match="timeout must be positive"):
            world.queue().task(timeout=timeout)


class PaymentDeclined(Exception):
    pass


class TestWhichErrorsRetry:
    async def run_failing(self, world: World, error: Exception, **options: Any) -> list[int]:
        queue = world.queue()
        runs: list[int] = []

        @queue.task("job", retries=3, backoff=0.01, **options)
        async def job() -> None:
            runs.append(1)
            raise error

        async with queue:
            await queue.enqueue(job)
            await eventually(queue.dead_letters)
        return runs

    async def test_give_up_on(self, world: World) -> None:
        runs = await self.run_failing(world, ValueError("bad data"), give_up_on=(ValueError,))
        assert len(runs) == 1

    async def test_retry_on(self, world: World) -> None:
        runs = await self.run_failing(world, PaymentDeclined(), retry_on=ConnectionError)
        assert len(runs) == 1
        runs = await self.run_failing(World(), ConnectionError(), retry_on=(ConnectionError,))
        assert len(runs) == 4

    async def test_give_up_on_wins(self, world: World) -> None:
        runs = await self.run_failing(
            world,
            ConnectionRefusedError(),
            retry_on=(ConnectionError,),
            give_up_on=(ConnectionRefusedError,),
        )
        assert len(runs) == 1

    async def test_timeouts_follow_them(self, world: World) -> None:
        queue = world.queue()
        runs: list[int] = []

        @queue.task("job", retries=3, timeout=0.05, retry_on=(ConnectionError,))
        async def job() -> None:
            runs.append(1)
            await asyncio.sleep(1)

        async with queue:
            await queue.enqueue(job)
            await eventually(queue.dead_letters)
        assert len(runs) == 1  # a TimeoutError isn't a ConnectionError

    @pytest.mark.parametrize(
        "options",
        [{"retry_on": "ValueError"}, {"give_up_on": (ValueError, 1)}, {"retry_on": [int]}],
    )
    def test_invalid(self, world: World, options: dict[str, Any]) -> None:
        with pytest.raises(TypeError, match="exception type"):
            world.queue().task(**options)


class TestUniqueKeys:
    async def test_a_task_enqueued_twice_runs_once(self, world: World) -> None:
        queue = world.queue()
        runs: list[int] = []
        release = asyncio.Event()

        @queue.task("invoice")
        async def invoice(number: int) -> None:
            runs.append(number)
            await release.wait()

        first = await queue.enqueue(invoice, 42, unique_key="invoice-42")
        assert await queue.enqueue(invoice, 42, unique_key="invoice-42") == first  # waiting
        other = await queue.enqueue(invoice, 43, unique_key="invoice-43")
        assert other != first
        assert await queue.pending() == 2
        async with queue:
            await eventually(lambda: sorted(runs) == [42, 43])
            assert await state(queue, first) == "running"
            assert await queue.enqueue(invoice, 42, unique_key="invoice-42") == first  # running
            release.set()
            await eventually(lambda: settled(queue))
            again = await queue.enqueue(invoice, 42, unique_key="invoice-42")  # done: free
            assert again != first
            await eventually(lambda: settled(queue))
        assert sorted(runs) == [42, 42, 43]

    async def test_held_while_it_waits_for_a_retry_freed_by_the_dead_letters(
        self,
        world: World,
    ) -> None:
        queue = world.queue()
        runs: list[int] = []

        @queue.task("job", retries=1, backoff=0.3)
        async def job() -> None:
            runs.append(1)
            raise ConnectionError

        async with queue:
            first = await queue.enqueue(job, unique_key="k")
            await eventually(lambda: runs == [1])
            await eventually(lambda: state(queue, first), timeout=1)
            assert await state(queue, first) in ("retrying", "running")
            assert await queue.enqueue(job, unique_key="k") == first
            await eventually(queue.dead_letters)
        assert await queue.enqueue(job, unique_key="k") != first

    async def test_with_a_delay(self, world: World) -> None:
        queue = world.queue()

        @queue.task("job")
        async def job() -> None: ...

        first = await queue.enqueue(job, delay=60, unique_key="k")
        assert await queue.enqueue(job, unique_key="k") == first
        assert await queue.pending() == 1

    async def test_concurrent_enqueues(self, world: World) -> None:
        queues = [world.queue(world.cache()) for _ in range(5)]
        for queue in queues:
            queue.task("job")(_noop)
        ids = await asyncio.gather(
            *(queue.enqueue("job", unique_key="once") for queue in queues for _ in range(4))
        )
        assert len(set(ids)) == 1
        assert await queues[0].pending() == 1

    async def test_a_full_queue_still_answers_with_the_holder(self, world: World) -> None:
        queue = world.queue(max_length=1)
        queue.task("job")(_noop)
        first = await queue.enqueue("job", unique_key="k")
        assert await queue.enqueue("job", unique_key="k") == first
        with pytest.raises(QueueFull):
            await queue.enqueue("job", unique_key="other")

    async def test_the_key_isnt_stored_in_clear(self, world: World) -> None:
        queue = world.queue()
        queue.task("job")(_noop)
        await queue.enqueue("job", unique_key="ana@example.com")
        keys = [key async for key in world.raw.scan_iter()]
        values = [await world.raw.dump(key) for key in keys]
        assert not any(b"ana@example.com" in key for key in keys)
        assert not any(b"ana@example.com" in (value or b"") for value in values)

    @pytest.mark.parametrize("unique_key", ["", 42])
    async def test_invalid(self, world: World, unique_key: Any) -> None:
        queue = world.queue()
        queue.task("job")(_noop)
        with pytest.raises(ValueError, match="unique_key"):
            await queue.enqueue("job", unique_key=unique_key)

    @pytest.mark.parametrize("unique", ["UPPERCASE" * 4, 7, "abc"])
    async def test_a_malformed_digest_is_refused(self, world: World, unique: Any) -> None:
        queue = world.queue()
        queue.task("job")(_noop)
        await world.plant(queue, {"p": sealed(queue, {**body("job"), "unique": unique})})
        async with queue:
            await eventually(queue.dead_letters)
        assert [dead.reason for dead in await queue.dead_letters()] == ["malformed"]


async def _noop() -> None:
    pass


class TestStatus:
    async def test_every_state_of_a_task(self, world: World) -> None:
        queue = world.queue()
        release = asyncio.Event()
        failures = [1]

        @queue.task("job", retries=1, backoff=0.2)
        async def job() -> None:
            await release.wait()
            if failures[0]:
                failures[0] -= 1
                raise PaymentDeclined

        before = time.time()
        task_id = await queue.enqueue(job)
        found = await queue.status(task_id)
        assert isinstance(found, TaskStatus)
        assert (found.id, found.task, found.state, found.attempts) == (task_id, "job", "pending", 0)
        assert (found.error, found.reason) == (None, None)
        assert found.enqueued_at is not None
        assert before - 1 <= found.enqueued_at.timestamp() <= time.time() + 1
        async with queue:
            await eventually(lambda: _is(queue, task_id, "running"))
            release.set()
            await eventually(lambda: _is(queue, task_id, "retrying"))
            found = await queue.status(task_id)
            assert found is not None
            assert (found.attempts, found.error) == (1, f"{__name__}.PaymentDeclined")
            await eventually(lambda: _is(queue, task_id, "succeeded"))
        found = await queue.status(task_id)
        assert found is not None
        assert found.attempts == 2
        assert found.updated_at is not None
        assert found.enqueued_at is not None
        assert found.updated_at >= found.enqueued_at
        assert set(STATES) >= {found.state}

    async def test_no_arguments_in_it(self, world: World) -> None:
        queue = world.queue()
        queue.task("charge")(_charge)
        task_id = await queue.enqueue("charge", "4111-1111-1111-1111")
        record = await world.raw.hgetall(f"{queue._status}{task_id}")
        # Its dashes keep it from matching the task's ID, in hex, by chance.
        assert b"4111-1111" not in b"".join(record.values())
        assert "4111-1111" not in repr(await queue.status(task_id))

    async def test_it_expires_after_the_task_ends(self, world: World) -> None:
        queue = world.queue(status_ttl=60, replay_window=30)
        queue.task("job")(_noop)
        task_id = await queue.enqueue("job")
        ttl = await world.raw.pttl(f"{queue._status}{task_id}")
        assert ttl > queue.replay_window * 1000  # the task's deadline, and then status_ttl
        async with queue:
            await eventually(lambda: _is(queue, task_id, "succeeded"))
        assert 30_000 < await world.raw.pttl(f"{queue._status}{task_id}") <= 60_000

    async def test_it_lasts_as_long_as_the_payload(self, world: World) -> None:
        """What tells a copy of a task that ran from a dead letter: kept while a copy could be
        valid, even with a short status_ttl."""
        queue = world.queue(status_ttl=60)
        queue.task("job")(_noop)
        task_id = await queue.enqueue("job")
        async with queue:
            await eventually(lambda: _is(queue, task_id, "succeeded"))
        ttl = await world.raw.pttl(f"{queue._status}{task_id}")
        assert ttl > (queue.replay_window - 60) * 1000

    async def test_unknown_and_malformed_ids(self, world: World) -> None:
        queue = world.queue()
        assert await queue.status(uuid.uuid4().hex) is None
        assert not await queue.cancel(uuid.uuid4().hex)
        wrongs: list[Any] = ["", "x" * 32, "a" * 31, f"{'a' * 32}\n", 42]
        for wrong in wrongs:
            with pytest.raises(ValueError, match="isn't the ID of a task"):
                await queue.status(wrong)

    async def test_a_dead_letter_requeued(self, world: World) -> None:
        queue = world.queue()

        @queue.task("job", retries=0)
        async def job() -> None:
            raise PaymentDeclined

        async with queue:
            task_id = await queue.enqueue(job)
            await eventually(queue.dead_letters)
            assert await state(queue, task_id) == "dead"
            fails = queue._tasks["job"]
            queue._tasks["job"] = type(fails)(**{**fails.__dict__, "func": _noop})
            assert await queue.requeue_dead_letters() == 1
            assert await state(queue, task_id) in ("pending", "running", "succeeded")
            await eventually(lambda: _is(queue, task_id, "succeeded"))
        found = await queue.status(task_id)
        assert found is not None
        assert (found.attempts, found.reason) == (1, None)

    async def test_refused_tasks(self, world: World) -> None:
        queue = world.queue()
        planted = body("unknown")
        await world.plant(queue, {"p": sealed(queue, planted)})
        await world.plant(queue, {"p": b"not a task"})
        async with queue:
            await eventually(lambda: len_dead(queue, 2))
        found = await queue.status(planted["id"])
        assert found is not None
        assert (found.state, found.reason) == ("dead", "unknown_task")

    async def test_a_copy_doesnt_change_the_real_task_s_status(self, world: World) -> None:
        queue = world.queue()
        queue.task("job")(_noop)
        original = body("job")
        payload = sealed(queue, original)
        await world.plant(queue, {"p": payload})
        async with queue:
            await eventually(lambda: _is(queue, original["id"], "succeeded"))
            await world.plant(queue, {"p": payload})  # written back: a replay
            await eventually(queue.dead_letters)
        assert [dead.reason for dead in await queue.dead_letters()] == ["replayed"]
        assert await state(queue, original["id"]) == "succeeded"

    async def test_a_task_without_a_record_gets_one_when_it_runs(self, world: World) -> None:
        queue = world.queue()
        release = asyncio.Event()

        @queue.task("job")
        async def job() -> None:
            await release.wait()

        planted = body("job")
        await world.plant(queue, {"p": sealed(queue, planted)})
        assert await queue.status(planted["id"]) is None  # written by hand: no record
        async with queue:
            await eventually(lambda: _is(queue, planted["id"], "running"))
            found = await queue.status(planted["id"])
            assert found is not None
            assert found.enqueued_at is None
            release.set()
            await eventually(lambda: _is(queue, planted["id"], "succeeded"))

    async def test_a_record_someone_else_wrote(self, world: World) -> None:
        queue = world.queue()
        queue._ready()
        task_id = uuid.uuid4().hex
        await world.raw.hset(
            f"{queue._status}{task_id}",
            mapping={
                "state": "running",
                "attempts": "many",
                "enqueued": "soon",
                "updated": "1e999",
            },
        )
        found = await queue.status(task_id)
        assert found is not None
        assert (found.attempts, found.enqueued_at, found.updated_at) == (0, None, None)
        assert (found.task, found.error) == ("", None)
        assert tasks_module._fields(None) == {}

    async def test_interrupted_tasks_are_pending_again(self, world: World) -> None:
        queue = world.queue(grace=0.05)

        @queue.task("job")
        async def job() -> None:
            await asyncio.Event().wait()

        await queue.start()
        task_id = await queue.enqueue(job)
        await eventually(lambda: _is(queue, task_id, "running"))
        await queue.stop()
        assert await state(queue, task_id) == "pending"


async def _charge(card: str) -> None:
    del card


async def _is(queue: FastAPITaskQueue, task_id: str, expected: str) -> bool:
    return await state(queue, task_id) == expected


async def len_dead(queue: FastAPITaskQueue, count: int) -> bool:
    return len(await queue.dead_letters()) == count


class TestCancel:
    async def test_a_pending_task(self, world: World) -> None:
        queue = world.queue()
        runs: list[int] = []

        @queue.task("job")
        async def job() -> None:
            runs.append(1)

        task_id = await queue.enqueue(job, unique_key="k")
        assert await queue.cancel(task_id)
        assert await state(queue, task_id) == "cancelled"
        assert not await queue.cancel(task_id)  # once
        assert await queue.pending() == 1  # until a worker drops it
        again = await queue.enqueue(job, unique_key="k")  # its key is free at once
        assert again != task_id
        async with queue:
            await eventually(lambda: settled(queue))
        assert runs == [1]  # the second one only
        assert await state(queue, task_id) == "cancelled"
        assert await state(queue, again) == "succeeded"

    async def test_a_delayed_task(self, world: World) -> None:
        queue = world.queue()
        runs: list[int] = []

        @queue.task("job")
        async def job() -> None:
            runs.append(1)

        task_id = await queue.enqueue(job, delay=0.2)
        assert await queue.cancel(task_id)
        async with queue:
            await asyncio.sleep(0.4)
            await eventually(lambda: settled(queue))
        assert runs == []

    async def test_a_task_waiting_for_a_retry(self, world: World) -> None:
        queue = world.queue()
        runs: list[int] = []

        @queue.task("job", retries=3, backoff=0.3)
        async def job() -> None:
            runs.append(1)
            raise ConnectionError

        async with queue:
            task_id = await queue.enqueue(job)
            await eventually(lambda: _is(queue, task_id, "retrying"))
            assert await queue.cancel(task_id)
            await asyncio.sleep(0.5)
            await eventually(lambda: settled(queue))
        assert runs == [1]
        assert await queue.dead_letters() == []

    async def test_running_and_finished_tasks_arent(self, world: World) -> None:
        queue = world.queue()
        release = asyncio.Event()

        @queue.task("job")
        async def job() -> None:
            await release.wait()

        async with queue:
            task_id = await queue.enqueue(job)
            await eventually(lambda: _is(queue, task_id, "running"))
            assert not await queue.cancel(task_id)
            release.set()
            await eventually(lambda: _is(queue, task_id, "succeeded"))
            assert not await queue.cancel(task_id)

    async def test_the_claim_checks_it(self, world: World) -> None:
        """Cancelling and a worker starting the task: one of them wins, in one script each."""
        queue = world.queue()
        queue.task("job")(_noop)
        task_id = await queue.enqueue("job")
        assert await queue.cancel(task_id)
        key = f"{queue._runs}{uuid.uuid4().hex}"
        status = f"{queue._status}{task_id}"
        args = ("1-0", "1000", "job", "1", "0", "1000")
        assert await world.raw.eval(CLAIM_SCRIPT, 2, key, status, *args) == -1
        assert not await world.raw.exists(key)  # the run isn't claimed either

    async def test_a_record_with_a_strange_unique(self, world: World) -> None:
        queue = world.queue()
        queue.task("job")(_noop)
        task_id = await queue.enqueue("job")
        await world.raw.hset(f"{queue._status}{task_id}", "unique", "}{other")
        assert await queue.cancel(task_id)


class TestDeadLetterCount:
    async def test_count(self, world: World) -> None:
        queue = world.queue()
        assert await queue.dead_letter_count() == 0
        await world.plant(queue, {"p": b"x"})
        await world.plant(queue, {"p": b"y"})
        async with queue:
            await eventually(lambda: len_dead(queue, 2))
        assert await queue.dead_letter_count() == 2


async def test_status_ttl_must_be_positive(world: World) -> None:
    with pytest.raises(ValueError, match="status_ttl"):
        world.queue(status_ttl=0)


async def test_the_body_carries_the_digest(world: World) -> None:
    queue = world.queue(world.cache(signing_key=None))
    queue.task("job")(_noop)
    await queue.enqueue("job", unique_key="k")
    (entry,) = await world.raw.xrange(queue._stream)
    assert len(json.loads(entry[1][b"p"])["unique"]) == 32
