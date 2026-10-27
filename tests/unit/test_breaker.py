"""FastAPIBreaker: a circuit breaker for any call to a dependency."""

import asyncio
import logging
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from fastapi_alcazaba import BreakerOpen, FastAPIBreaker, FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.telemetry import Metrics
from tests.unit.test_metrics import points


class Dependency:
    """A dependency that fails while `down`, counting the calls that reached it."""

    def __init__(self) -> None:
        self.down = False
        self.calls = 0

    async def call(self) -> str:
        self.calls += 1
        if self.down:
            raise ConnectionError("down")
        return "ok"


async def fail(breaker: FastAPIBreaker, dependency: Dependency, times: int) -> None:
    guarded = breaker(dependency.call)
    for _ in range(times):
        with pytest.raises(ConnectionError):
            await guarded()


class TestStates:
    async def test_consecutive_failures_open_it(self) -> None:
        breaker, dependency = FastAPIBreaker("db", failure_threshold=3), Dependency()
        dependency.down = True
        await fail(breaker, dependency, 3)
        assert breaker.state == "open"
        with pytest.raises(BreakerOpen) as refused:
            await breaker(dependency.call)()
        assert dependency.calls == 3  # the last call didn't reach it
        assert refused.value.name == "db"
        assert 29 < refused.value.retry_after <= 30

    async def test_a_success_starts_the_count_again(self) -> None:
        breaker, dependency = FastAPIBreaker("db", failure_threshold=2), Dependency()
        guarded = breaker(dependency.call)
        dependency.down = True
        await fail(breaker, dependency, 1)
        dependency.down = False
        assert await guarded() == "ok"
        dependency.down = True
        await fail(breaker, dependency, 1)
        assert breaker.state == "closed"

    async def test_a_trial_after_the_reset_timeout(self) -> None:
        breaker = FastAPIBreaker("db", failure_threshold=1, reset_timeout=0.05)
        dependency = Dependency()
        dependency.down = True
        await fail(breaker, dependency, 1)
        await asyncio.sleep(0.06)
        assert (breaker.state, breaker.retry_after) == ("half-open", 0)
        await fail(breaker, dependency, 1)  # the trial fails: open again
        assert breaker.state == "open"
        await asyncio.sleep(0.06)
        dependency.down = False
        assert await breaker(dependency.call)() == "ok"
        assert (breaker.state, breaker.retry_after) == ("closed", 0)

    async def test_one_trial_at_a_time(self) -> None:
        breaker = FastAPIBreaker("db", failure_threshold=1, reset_timeout=0.05)
        gate = asyncio.Event()

        @breaker
        async def slow() -> None:
            await gate.wait()

        @breaker
        async def broken() -> None:
            raise ConnectionError

        with pytest.raises(ConnectionError):
            await broken()
        await asyncio.sleep(0.06)
        trial = asyncio.create_task(slow())
        await asyncio.sleep(0)
        with pytest.raises(BreakerOpen):
            await slow()
        gate.set()
        await trial
        assert breaker.state == "closed"

    async def test_reset_closes_it_and_forgets_the_failures(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reader = InMemoryMetricReader()
        metrics = Metrics(meter_provider=MeterProvider([reader]))
        breaker = FastAPIBreaker("db", failure_threshold=2, reset_timeout=60, metrics=metrics)
        assert (breaker.failure_threshold, breaker.reset_timeout) == (2, 60)
        dependency = Dependency()
        dependency.down = True
        await fail(breaker, dependency, 2)
        assert breaker.state == "open"
        with caplog.at_level(logging.INFO, "fastapi_alcazaba.operations.breaker"):
            breaker.reset()
            breaker.reset()  # closed already: nothing to tell
        assert (breaker.state, breaker.retry_after) == ("closed", 0.0)
        assert caplog.text.count("Circuit breaker 'db' closed by reset()") == 1
        assert [state for state, _ in points(reader, TRANSITIONS)] == [
            {"fastapi_alcazaba.state": "open", "fastapi_alcazaba.breaker": "db"},
            {"fastapi_alcazaba.state": "closed", "fastapi_alcazaba.breaker": "db"},
        ]
        await fail(breaker, dependency, 1)  # counting from 0 again: one failure doesn't open it
        assert breaker.state == "closed"


class TestWhatCounts:
    async def test_excluded_exceptions_are_successes(self) -> None:
        breaker = FastAPIBreaker("api", failure_threshold=2, exclude=(LookupError,))

        @breaker
        async def find() -> None:
            raise KeyError("not found")  # the dependency answered

        @breaker
        async def broken() -> None:
            raise ConnectionError

        for call in (broken, find, broken):
            with pytest.raises((KeyError, ConnectionError)):
                await call()
        assert breaker.state == "closed"

    async def test_errors_outside_failures_give_no_verdict(self) -> None:
        breaker = FastAPIBreaker(
            "api", failure_threshold=2, reset_timeout=0.05, failures=(ConnectionError,)
        )

        @breaker
        async def mistaken() -> None:
            raise ValueError("the caller's own")

        @breaker
        async def broken() -> None:
            raise ConnectionError

        for call in (broken, mistaken, broken):  # the ValueError doesn't reset the count
            with pytest.raises((ValueError, ConnectionError)):
                await call()
        assert breaker.state == "open"
        await asyncio.sleep(0.06)
        with pytest.raises(ValueError, match="own"):
            await mistaken()  # the trial, without a verdict
        assert breaker.state == "half-open"  # the next call may try

    async def test_a_cancelled_trial_lets_the_next_call_try(self) -> None:
        breaker = FastAPIBreaker("db", failure_threshold=1, reset_timeout=0.05)

        @breaker
        async def hangs() -> None:
            await asyncio.sleep(10)

        @breaker
        async def broken() -> None:
            raise ConnectionError

        with pytest.raises(ConnectionError):
            await broken()
        await asyncio.sleep(0.06)
        trial = asyncio.create_task(hangs())
        await asyncio.sleep(0)
        trial.cancel()
        with pytest.raises(asyncio.CancelledError):
            await trial
        assert breaker.state == "half-open"
        with pytest.raises(ConnectionError):
            await broken()


class TestTimeout:
    async def test_a_slow_call_is_a_failure(self) -> None:
        breaker = FastAPIBreaker("db", failure_threshold=1, timeout=0.05, exclude=(TimeoutError,))

        @breaker
        async def slow() -> None:
            await asyncio.sleep(10)

        with pytest.raises(TimeoutError):
            await slow()
        assert breaker.state == "open"  # its own timeout counts, excluded or not

    async def test_fast_calls_go_through(self) -> None:
        breaker = FastAPIBreaker("db", timeout=1)
        async with breaker.attempt():
            await asyncio.sleep(0)
        assert breaker.state == "closed"

    def test_sync_code_cant_be_interrupted(self) -> None:
        breaker = FastAPIBreaker("db", timeout=1)
        with pytest.raises(TypeError, match="sync: a timeout can't interrupt it"):
            breaker(lambda: None)
        with pytest.raises(TypeError, match="use `async with`"), breaker.attempt():
            pass

    def test_validation(self) -> None:
        with pytest.raises(ValueError, match="timeout"):
            FastAPIBreaker("db", timeout=0)
        with pytest.raises(ValueError, match="failure_threshold"):
            FastAPIBreaker("db", failure_threshold=0)


class TestUsage:
    def test_sync_functions(self) -> None:
        breaker = FastAPIBreaker("sdk", failure_threshold=2)

        @breaker
        def call(failing: bool) -> str:
            """Docs."""
            if failing:
                raise ConnectionError
            return "ok"

        assert (call(failing=False), call.__name__, call.__doc__) == ("ok", "call", "Docs.")
        for _ in range(2):
            with pytest.raises(ConnectionError):
                call(failing=True)
        with pytest.raises(BreakerOpen):
            call(failing=False)

    def test_threads(self) -> None:
        """FastAPI runs sync endpoints in a thread pool."""
        breaker = FastAPIBreaker("sdk", failure_threshold=50)
        errors: list[BaseException] = []

        @breaker
        def broken() -> None:
            raise ConnectionError

        def worker() -> None:
            for _ in range(10):
                try:
                    broken()
                except (ConnectionError, BreakerOpen) as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(isinstance(error, ConnectionError) for error in errors) == 50
        assert breaker.state == "open"

    async def test_around_a_block(self) -> None:
        breaker = FastAPIBreaker("db", failure_threshold=1)
        with pytest.raises(ConnectionError):
            async with breaker.attempt():
                raise ConnectionError
        with pytest.raises(BreakerOpen), breaker.attempt():
            pass

    def test_fastapi_answers_503_with_retry_after(self) -> None:
        breaker = FastAPIBreaker("payments", failure_threshold=1, reset_timeout=30)
        app = FastAPI()

        @app.get("/pay")
        @breaker
        async def pay() -> None:
            raise ConnectionError

        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/pay").status_code == 500
        refused = client.get("/pay")
        assert (refused.status_code, refused.json()) == (503, {"detail": "Service Unavailable"})
        assert refused.headers["retry-after"] == "30"
        assert str(BreakerOpen("payments", 0.2)) == "the circuit breaker 'payments' is open"
        assert BreakerOpen("payments", 0.2).headers == {"Retry-After": "1"}


class TestObservability:
    async def test_transitions_are_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        breaker = FastAPIBreaker("db", failure_threshold=1, reset_timeout=0.05)
        dependency = Dependency()
        dependency.down = True
        with caplog.at_level(logging.INFO, "fastapi_alcazaba.operations.breaker"):
            await fail(breaker, dependency, 1)
            await asyncio.sleep(0.06)
            dependency.down = False
            await breaker(dependency.call)()
        assert "Circuit breaker 'db' open: calls refused for 0.05s" in caplog.text
        assert "Circuit breaker 'db' closed" in caplog.text

    async def test_metrics(self) -> None:
        reader = InMemoryMetricReader()
        metrics = Metrics(meter_provider=MeterProvider([reader]))
        breaker = FastAPIBreaker("db", failure_threshold=1, metrics=metrics)
        dependency = Dependency()
        assert await breaker(dependency.call)() == "ok"
        dependency.down = True
        await fail(breaker, dependency, 1)
        with pytest.raises(BreakerOpen):
            await breaker(dependency.call)()
        calls = {a["fastapi_alcazaba.outcome"]: n for a, n in points(reader, CALLS)}
        assert calls == {"success": 1, "failure": 1, "refused": 1}
        assert points(reader, TRANSITIONS) == [
            ({"fastapi_alcazaba.state": "open", "fastapi_alcazaba.breaker": "db"}, 1)
        ]

    async def test_fastapi_caches_metrics_by_default(self) -> None:
        reader = InMemoryMetricReader()
        FastAPICache.init(InMemoryBackend(), meter_provider=MeterProvider([reader]))
        breaker = FastAPIBreaker("db")
        await breaker(Dependency().call)()
        assert points(reader, CALLS) == [
            ({"fastapi_alcazaba.outcome": "success", "fastapi_alcazaba.breaker": "db"}, 1)
        ]


CALLS = "fastapi_alcazaba.breaker.calls"
TRANSITIONS = "fastapi_alcazaba.breaker.transitions"
