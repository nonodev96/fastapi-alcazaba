import asyncio
import time

import pytest

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.core import BackendGuard, BackendUnavailable, CircuitBreaker, CircuitOpen
from fastapi_alcazaba.response_cache import cache
from fastapi_alcazaba.telemetry import Metrics


class TestCircuitBreaker:
    def test_opens_after_consecutive_failures(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, reset_timeout=30)
        breaker.record_failure()
        assert breaker.state == "closed"
        breaker.record_success()
        breaker.record_failure()
        assert breaker.allow()  # a success in between resets the count
        breaker.record_failure()
        assert breaker.state == "open"
        assert not breaker.allow()

    def test_half_open_lets_one_trial_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout=10)
        breaker.record_failure()
        now = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: now + 11)
        assert breaker.state == "half-open"
        assert breaker.allow()
        assert not breaker.allow()  # only one trial at a time
        breaker.record_success()
        assert breaker.state == "closed"

    def test_a_failed_trial_opens_it_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        breaker = CircuitBreaker(failure_threshold=3, reset_timeout=10)
        for _ in range(3):
            breaker.record_failure()
        now = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: now + 11)
        assert breaker.allow()
        breaker.record_failure()
        assert breaker.state == "open"

    def test_allow_says_whether_the_call_is_the_trial(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout=10)
        assert breaker.allow() == "closed"
        breaker.record_failure()
        assert breaker.allow() is None
        now = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: now + 11)
        assert breaker.allow() == "half-open"
        assert breaker.allow() is None

    def test_an_abandoned_trial_does_not_block_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout=10)
        breaker.record_failure()
        now = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: now + 11)
        assert breaker.allow()
        breaker.abandon_trial()
        assert breaker.allow()

    @pytest.mark.parametrize(("threshold", "reset"), [(0, 1.0), (1, 0.0)])
    def test_invalid_settings(self, threshold: int, reset: float) -> None:
        with pytest.raises(ValueError, match="must be"):
            CircuitBreaker(threshold, reset)


class TestBackendGuard:
    async def test_timeout(self) -> None:
        guard = BackendGuard(0.05, None, Metrics(enabled=False))

        async def slow() -> int:
            await asyncio.sleep(1)
            return 1

        start = time.perf_counter()
        with pytest.raises(BackendUnavailable):
            await guard.run("get", slow)
        assert time.perf_counter() - start < 0.5

    async def test_failures_open_the_breaker_and_stop_the_calls(self) -> None:
        calls: list[int] = []
        guard = BackendGuard(None, CircuitBreaker(2, 30), Metrics(enabled=False))

        async def failing() -> None:
            calls.append(1)
            raise ConnectionError("down")

        for _ in range(2):
            with pytest.raises(BackendUnavailable):
                await guard.run("get", failing)
        with pytest.raises(CircuitOpen):
            await guard.run("get", failing)
        assert len(calls) == 2

    async def test_a_successful_trial_closes_the_breaker(self) -> None:
        breaker = CircuitBreaker(1, 0.01)
        guard = BackendGuard(None, breaker, Metrics(enabled=False))

        async def broken() -> None:
            raise ConnectionError("down")

        async def works() -> int:
            return 1

        with pytest.raises(BackendUnavailable):
            await guard.run("get", broken)
        await asyncio.sleep(0.02)
        assert await guard.run("get", works) == 1
        assert breaker.state == "closed"
        assert await BackendGuard(None, None, Metrics(enabled=False)).run("get", works) == 1

    async def test_cancellation_abandons_the_trial(self) -> None:
        breaker = CircuitBreaker(1, 30)
        guard = BackendGuard(None, breaker, Metrics(enabled=False))

        async def hangs() -> None:
            await asyncio.sleep(10)

        task = asyncio.create_task(guard.run("get", hangs))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert breaker.state == "closed"

    async def test_cancelling_a_call_that_isnt_the_trial_keeps_it(self) -> None:
        """A call sent while it was closed and cancelled during the trial doesn't let a second
        trial through."""
        guard = BackendGuard(None, CircuitBreaker(1, 0.01), Metrics(enabled=False))

        async def hangs() -> None:
            await asyncio.sleep(10)

        async def broken() -> None:
            raise ConnectionError("down")

        before = asyncio.create_task(guard.run("get", hangs))
        await asyncio.sleep(0)
        with pytest.raises(BackendUnavailable):
            await guard.run("get", broken)
        await asyncio.sleep(0.02)
        trial = asyncio.create_task(guard.run("get", hangs))
        await asyncio.sleep(0)
        before.cancel()
        with pytest.raises(asyncio.CancelledError):
            await before
        with pytest.raises(CircuitOpen):
            await guard.run("get", hangs)
        trial.cancel()
        with pytest.raises(asyncio.CancelledError):
            await trial

    def test_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="backend_timeout"):
            BackendGuard(0, None, Metrics(enabled=False))


class SlowBackend(InMemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
        self.calls += 1
        await asyncio.sleep(1)
        return 0, None

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        self.calls += 1
        await asyncio.sleep(1)


async def test_a_hanging_backend_does_not_slow_the_endpoint() -> None:
    backend = SlowBackend()
    FastAPICache.init(backend, backend_timeout=0.05, circuit_breaker_failures=2)

    @cache(expire=60)
    async def value() -> int:
        return 42

    start = time.perf_counter()
    assert await value() == 42  # get and set time out
    assert await value() == 42  # the breaker is open: the backend isn't called
    assert await value() == 42
    assert time.perf_counter() - start < 0.5
    assert backend.calls == 2


async def test_breaker_can_be_disabled() -> None:
    FastAPICache.init(InMemoryBackend(), circuit_breaker_failures=None, backend_timeout=None)
    assert FastAPICache.get_guard().breaker is None
    assert FastAPICache.get_guard().timeout is None
