"""Timeouts and a circuit breaker for backend calls: a slow or broken backend must not slow down
every request, the cache is skipped instead (fail open)."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from fastapi_alcazaba.errors import BackendUnavailable, CircuitOpen
from fastapi_alcazaba.metrics import Metrics
from fastapi_alcazaba.tracing import Tracer

T = TypeVar("T")


class CircuitBreaker:
    """Stops calling a failing backend for a while.

    Closed: calls go through, and `failure_threshold` consecutive failures open it. Open: calls are
    refused for `reset_timeout` seconds. Then a single trial call goes through (half open): a
    success closes it, a failure opens it again. One breaker per process.
    """

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 30.0) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if reset_timeout <= 0:
            raise ValueError("reset_timeout must be positive")
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_running = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at < self.reset_timeout:
            return "open"
        return "half-open"

    @property
    def retry_after(self) -> float:
        """Seconds until a trial call may go through; 0 unless it's open."""
        if self._opened_at is None:
            return 0.0
        return max(0.0, self.reset_timeout - (time.monotonic() - self._opened_at))

    def allow(self) -> str | None:
        """The state the call goes through in: "closed", or "half-open" when it's the single
        trial; None when it's refused."""
        if self._opened_at is None:
            return "closed"
        if self._trial_running or time.monotonic() - self._opened_at < self.reset_timeout:
            return None
        self._trial_running = True
        return "half-open"

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._trial_running = False

    def record_failure(self) -> None:
        self._failures += 1
        self._trial_running = False
        if self._opened_at is not None or self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()

    def reset(self) -> None:
        """Close it, whatever its state: the failures counted so far are forgotten."""
        self._failures = 0
        self._opened_at = None
        self._trial_running = False

    def abandon_trial(self) -> None:
        """The trial call ended without a verdict (cancelled): let the next call try. Only for the
        call `allow()` let through "half-open": another one would let a second trial through."""
        self._trial_running = False


class BackendGuard:
    """Runs each backend call with a timeout and through the circuit breaker. Any failure becomes
    BackendUnavailable."""

    def __init__(
        self,
        timeout: float | None,
        breaker: CircuitBreaker | None,
        metrics: Metrics,
        tracer: Tracer | None = None,
    ) -> None:
        if timeout is not None and timeout <= 0:
            raise ValueError("backend_timeout must be positive (or None for no timeout)")
        self.timeout = timeout
        self.breaker = breaker
        self.metrics = metrics
        self.tracer = tracer if tracer is not None else Tracer(enabled=False)

    async def run(self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        breaker = self.breaker
        trial = False
        if breaker is not None:
            if (state := breaker.allow()) is None:
                self.metrics.backend_error(operation, "CircuitOpen")
                raise CircuitOpen(f"{operation}: the circuit breaker is open")
            trial = state == "half-open"
        start = time.perf_counter()
        try:
            with self.tracer.backend(operation):
                async with asyncio.timeout(self.timeout):
                    result = await call()
        except Exception as exc:
            if breaker is not None:
                breaker.record_failure()
            self.metrics.backend_error(operation, type(exc).__name__)
            raise BackendUnavailable(f"{operation} failed: {exc!r}") from exc
        except BaseException:
            if trial and breaker is not None:
                breaker.abandon_trial()
            raise
        finally:
            self.metrics.backend_duration(operation, time.perf_counter() - start)
        if breaker is not None:
            breaker.record_success()
        return result
