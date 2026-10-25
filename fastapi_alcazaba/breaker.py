"""A circuit breaker for the calls an app makes to its dependencies: a database, an SDK, a service
reached without httpx (for httpx, ResilientTransport has one per origin).

    payments = FastAPIBreaker("payments", timeout=10)

    @payments
    async def charge(order: Order) -> Receipt: ...

After `failure_threshold` consecutive failures the breaker opens: for `reset_timeout` seconds
calls fail at once with `BreakerOpen` (a 503 with `Retry-After` when it reaches FastAPI) instead
of waiting for a dependency that is down, which gets room to recover. Then a single trial call
decides whether it's back. Each process has its own breakers: no round trip on every call, and a
process opens one after its own failures.
"""

import asyncio
import functools
import inspect
import logging
import math
import threading
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Any, ParamSpec, TypeVar, overload

from fastapi import HTTPException

from fastapi_alcazaba.metrics import Metrics
from fastapi_alcazaba.resilience import CircuitBreaker

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

P = ParamSpec("P")
T = TypeVar("T")

#: Exceptions that never give a verdict on the dependency: the call was interrupted.
_NO_VERDICT = (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit)


class BreakerOpen(HTTPException):
    """The breaker is open: the call wasn't made. FastAPI answers it with a 503 and `Retry-After`;
    the response doesn't name the dependency."""

    def __init__(self, name: str, retry_after: float) -> None:
        seconds = max(1, math.ceil(retry_after))
        super().__init__(503, "Service Unavailable", headers={"Retry-After": str(seconds)})
        self.name = name
        self.retry_after = retry_after

    def __str__(self) -> str:
        return f"the circuit breaker {self.name!r} is open"


class FastAPIBreaker:
    """A circuit breaker named `name` (for logs and metrics), as a decorator of async or sync
    functions, or around a block with `attempt()`:

        @breaker
        async def fetch(...): ...

        async with breaker.attempt():
            ...

    :param failure_threshold: consecutive failures that open it.
    :param reset_timeout: seconds it stays open before a trial call may go through.
    :param timeout: seconds an async call may take; past them it's cancelled, counts as a failure
        and raises TimeoutError. None for no limit. Sync code can't be interrupted: with a
        timeout, the breaker refuses to guard it.
    :param failures: the exceptions that count as failures of the dependency.
    :param exclude: exceptions that mean the dependency answered (a "not found" from its SDK):
        they count as successes, and go on to the caller like any other.
    :param metrics: where to count calls and transitions; FastAPICache's by default.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        timeout: float | None = None,
        failures: tuple[type[BaseException], ...] = (Exception,),
        exclude: tuple[type[BaseException], ...] = (),
        metrics: Metrics | None = None,
    ) -> None:
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive (or None for no limit)")
        self.name = name
        self.timeout = timeout
        self.failures = failures
        self.exclude = exclude
        self._metrics = metrics
        self._breaker = CircuitBreaker(failure_threshold, reset_timeout)
        # Sync functions run in the threads of FastAPI's pool.
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        """ "closed", "open" or "half-open" (a trial call may go through)."""
        return self._breaker.state

    @property
    def retry_after(self) -> float:
        """Seconds until a trial call may go through; 0 unless it's open."""
        return self._breaker.retry_after

    @overload
    def __call__(
        self, func: Callable[P, Coroutine[Any, Any, T]]
    ) -> Callable[P, Coroutine[Any, Any, T]]: ...

    @overload
    def __call__(self, func: Callable[P, T]) -> Callable[P, T]: ...

    def __call__(self, func: Callable[P, Any]) -> Callable[P, Any]:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def guarded_async(*args: P.args, **kwargs: P.kwargs) -> Any:
                async with self.attempt():
                    return await func(*args, **kwargs)

            return guarded_async
        if self.timeout is not None:
            raise TypeError(f"{func.__qualname__} is sync: a timeout can't interrupt it")

        @functools.wraps(func)
        def guarded(*args: P.args, **kwargs: P.kwargs) -> Any:
            with self.attempt():
                return func(*args, **kwargs)

        return guarded

    def attempt(self) -> "_Attempt":
        """A context manager for one call: `async with` (with the timeout) or `with`."""
        return _Attempt(self)

    def _metrics_now(self) -> Metrics:
        if self._metrics is not None:
            return self._metrics
        from fastapi_alcazaba import FastAPICache

        return FastAPICache.get_metrics()

    def _enter(self) -> bool:
        """Let the call through, or raise BreakerOpen; whether it's the trial."""
        with self._lock:
            state = self._breaker.allow()
            retry_after = self._breaker.retry_after
        if state is None:
            self._metrics_now().breaker_call("refused", self.name)
            raise BreakerOpen(self.name, retry_after)
        return state == "half-open"

    def _record(self, exc: BaseException | None, trial: bool, *, timed_out: bool = False) -> None:
        if timed_out:
            outcome: str | None = "failure"
        elif exc is None or isinstance(exc, self.exclude):
            outcome = "success"
        elif isinstance(exc, self.failures) and not isinstance(exc, _NO_VERDICT):
            outcome = "failure"
        else:
            outcome = None  # interrupted, or the caller's own error: no verdict
        with self._lock:
            before = self._breaker.state
            if outcome == "success":
                self._breaker.record_success()
            elif outcome == "failure":
                self._breaker.record_failure()
            elif trial:
                self._breaker.abandon_trial()  # the next call may try
            after = self._breaker.state
        metrics = self._metrics_now()
        if outcome is not None:
            metrics.breaker_call(outcome, self.name)
        if after == "open" and before != "open":
            logger.warning(
                "Circuit breaker %r open: calls refused for %ss",
                self.name,
                self._breaker.reset_timeout,
            )
            metrics.breaker_transition("open", self.name)
        elif after == "closed" and before != "closed":
            logger.info("Circuit breaker %r closed: the dependency answers again", self.name)
            metrics.breaker_transition("closed", self.name)


class _Attempt:
    """One call through the breaker; a new one for every call, since calls run concurrently."""

    def __init__(self, breaker: FastAPIBreaker) -> None:
        self._breaker = breaker
        self._trial = False
        self._deadline: asyncio.Timeout | None = None

    async def __aenter__(self) -> "_Attempt":
        self._trial = self._breaker._enter()
        if self._breaker.timeout is not None:
            self._deadline = asyncio.timeout(self._breaker.timeout)
            await self._deadline.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._deadline is not None:
            try:
                await self._deadline.__aexit__(exc_type, exc, traceback)
            except TimeoutError as timeout:
                self._breaker._record(timeout, self._trial, timed_out=True)
                raise
        self._breaker._record(exc, self._trial)

    def __enter__(self) -> "_Attempt":
        if self._breaker.timeout is not None:
            raise TypeError("a timeout can't interrupt sync code: use `async with`")
        self._trial = self._breaker._enter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._breaker._record(exc, self._trial)
