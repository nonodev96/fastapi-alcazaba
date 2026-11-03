"""Gauges of the work piling up, measured every so often: the tasks of a queue, the messages of
an outbox. Measuring costs a call to the backend or the database, so nothing is measured unless
the app starts a monitor:

    monitor = BacklogMonitor(queue, outbox, interval=30)
    app = FastAPI(lifespan=monitor.lifespan)

Each source sets its gauges (`tasks.backlog`, `tasks.dead_letters`, `outbox.backlog`,
`outbox.oldest_age`) through the metrics of FastAPICache, or of the queue's cache.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Protocol

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class Measured(Protocol):
    """What a BacklogMonitor measures: `FastAPITaskQueue`, `SQLAlchemyOutbox`, or yours."""

    def measure(self) -> Awaitable[None]: ...


class BacklogMonitor:
    """Measures its sources every `interval` seconds while it runs, each within `timeout`: a
    source that fails or hangs is logged, and the others are measured all the same.

    :param sources: what to measure: queues, outboxes (anything with `async measure()`).
    :param interval: seconds between two measures.
    :param timeout: seconds each source may take.
    """

    def __init__(self, *sources: Measured, interval: float = 30.0, timeout: float = 5.0) -> None:
        if not sources:
            raise ValueError("give the queues and outboxes to measure")
        if not all(callable(getattr(source, "measure", None)) for source in sources):
            raise TypeError("each source has an async measure(): a queue, an outbox")
        if interval <= 0 or timeout <= 0:
            raise ValueError("interval and timeout must be positive")
        self.sources = sources
        self.interval = interval
        self.timeout = timeout
        self._task: asyncio.Task[None] | None = None

    async def measure(self) -> None:
        """Measure every source once, now."""
        await asyncio.gather(*(self._measure(source) for source in self.sources))

    async def start(self) -> None:
        """Measure now, and then every `interval` seconds."""
        if self._task is not None:
            raise RuntimeError("the monitor is already running")
        self._task = asyncio.get_running_loop().create_task(self._run(), name="backlog monitor")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @asynccontextmanager
    async def lifespan(self, app: Any = None) -> AsyncIterator[None]:
        """For `FastAPI(lifespan=monitor.lifespan)`: measures while the app runs."""
        await self.start()
        try:
            yield
        finally:
            await self.stop()

    async def __aenter__(self) -> "BacklogMonitor":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def _run(self) -> None:
        while True:
            await self.measure()
            await asyncio.sleep(self.interval)

    async def _measure(self, source: Measured) -> None:
        try:
            async with asyncio.timeout(self.timeout):
                await source.measure()
        except Exception:
            logger.warning("Couldn't measure the backlog of %r", source, exc_info=True)
