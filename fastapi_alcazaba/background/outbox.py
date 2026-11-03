"""The transactional outbox: a message written in the same database transaction as the change it
announces, then delivered by a relay, at least once. A change can't be saved without its message,
nor a message sent for a change that was rolled back. Needs the `sqlalchemy` extra.

    outbox = SQLAlchemyOutbox(outbox_table(Base.metadata), sessions=async_session)

    async with async_session() as session, session.begin():
        session.add(order)
        await outbox.add(session, "order.placed", {"order_id": order.id})  # same transaction

    relay = OutboxRelay(outbox, deliver)  # deliver(message): to the task queue, a webhook...
    leader = FastAPILeader("outbox")      # one process relays

    @leader.run
    async def relay_outbox() -> None:
        await relay.run()

Each message has an id, which `deliver` gets: a message delivered, and then not marked as sent
(the process stopped between both), is delivered again; consumers keep the ids they've seen.

The relay deletes the messages sent more than `retention` ago (7 days); those it gave up on stay
until `requeue` or `discard`. Each delivery is a span linked to the one that wrote the message.
"""

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from starlette.concurrency import run_in_threadpool

from fastapi_alcazaba.telemetry.metrics import Metrics
from fastapi_alcazaba.telemetry.tracing import Tracer, record_failure
from fastapi_alcazaba.telemetry.tracing import traceparent as current_traceparent

try:
    from sqlalchemy import (
        Column,
        Float,
        Index,
        Integer,
        MetaData,
        String,
        Table,
        Text,
        and_,
        delete,
        func,
        select,
        update,
    )
    from sqlalchemy.orm import Session
    from sqlalchemy.sql.elements import ColumnElement
except ImportError as exc:  # pragma: no cover  # the extra isn't installed
    raise ImportError(
        'the outbox needs SQLAlchemy: pip install "fastapi-alcazaba[sqlalchemy]"'
    ) from exc
try:
    from sqlalchemy.ext.asyncio import AsyncSession
except ImportError:  # pragma: no cover  # greenlet isn't installed: no async sessions, then
    AsyncSession = None  # type: ignore[assignment,misc]

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: The longest payload, as JSON.
MAX_PAYLOAD = 256 * 1024
#: The length of a W3C traceparent (version 00).
_TRACEPARENT = 55


def outbox_table(metadata: MetaData, name: str = "outbox", *, traceparent: bool = True) -> Table:
    """The outbox's table, in your metadata (its migrations create it): each message's id, topic,
    payload (JSON), when it was written, the attempts at delivering it, when it may be tried
    again, when it was sent (or given up on), and the trace context of where it was written.

    :param traceparent: with the column `traceparent`, which links each delivery to the request
        that wrote the message; False for a table made before 0.8, without it.
    """
    columns = [
        Column("id", String(32), primary_key=True),
        Column("topic", String(200), nullable=False),
        Column("payload", Text, nullable=False),
        Column("created", Float, nullable=False),
        Column("attempts", Integer, nullable=False, default=0),
        Column("next_try", Float, nullable=False),
        Column("sent", Float, nullable=True),
        Column("failed", Float, nullable=True),
        Column("error", String(500), nullable=True),
    ]
    if traceparent:
        columns.append(Column("traceparent", String(_TRACEPARENT), nullable=True))
    return Table(
        name,
        metadata,
        *columns,
        # What the relay (sent IS NULL AND next_try <= now), the backlog (sent IS NULL) and the
        # purge (sent < cutoff) read: on next_try alone, every sent row would match the relay's.
        Index(f"ix_{name}_due", "sent", "next_try"),
        Index(f"ix_{name}_failed", "failed"),
    )


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """A message of the outbox, as `deliver` gets it.

    :param id: random, unique: what consumers deduplicate by.
    :param topic: what it's about, such as "order.placed".
    :param payload: what was written with it.
    :param attempts: the deliveries tried before this one.
    :param created: when it was written (Unix time).
    :param traceparent: the W3C trace context of where it was written, or None.
    """

    id: str
    topic: str
    payload: Any
    attempts: int
    created: float
    traceparent: str | None = None


@dataclass(frozen=True, slots=True)
class GivenUpMessage:
    """A message the relay gave up on, as `given_up()` lists it: its payload isn't shown.

    :param id: its id, for `requeue` and `discard`.
    :param topic: what it's about.
    :param attempts: the deliveries tried.
    :param created: when it was written (Unix time).
    :param failed: when the relay gave up (Unix time).
    :param error: the last delivery's error.
    """

    id: str
    topic: str
    attempts: int
    created: float
    failed: float
    error: str | None


#: A session of SQLAlchemy, plain or async.
AnySession = Any


def _observers() -> tuple[Metrics, Tracer]:
    """FastAPICache's metrics and tracer when it's set up; the global providers' otherwise."""
    from fastapi_alcazaba.response_cache.caches import FastAPICache

    try:
        cache = FastAPICache.instance()
    except RuntimeError:  # not set up: the app may use the outbox without the cache
        return Metrics(), Tracer()
    return cache.metrics, cache.tracer


class SQLAlchemyOutbox:
    """The outbox in a table of your database (`outbox_table`), written through your sessions.

    :param table: the outbox's table.
    :param sessions: makes the relay's sessions (a `sessionmaker` or an `async_sessionmaker`).
    """

    def __init__(self, table: Table, sessions: Callable[[], AnySession]) -> None:
        self.table = table
        self.sessions = sessions
        self._traced = "traceparent" in table.c

    async def add(self, session: AnySession, topic: str, payload: Any) -> str:
        """Write a message in `session`'s transaction: it's sent once the transaction commits,
        and never if it's rolled back. Returns its id."""
        if not isinstance(topic, str) or not 1 <= len(topic) <= 200:
            raise ValueError("a topic is 1 to 200 characters")
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        if len(body) > MAX_PAYLOAD:
            raise ValueError(f"a payload is {MAX_PAYLOAD} bytes of JSON at most")
        message_id = secrets.token_hex(16)
        now = time.time()
        values: dict[str, Any] = {
            "id": message_id,
            "topic": topic,
            "payload": body,
            "created": now,
            "attempts": 0,
            "next_try": now,
        }
        if self._traced and (context := current_traceparent()) and len(context) <= _TRACEPARENT:
            values["traceparent"] = context
        result = session.execute(self.table.insert().values(**values))
        if asyncio.iscoroutine(result):
            await result
        return message_id

    async def due(self, limit: int) -> list[OutboxMessage]:
        """The oldest messages not sent, whose time to be tried has come."""
        table = self.table
        query = (
            select(table)
            .where(and_(table.c.sent.is_(None), table.c.next_try <= time.time()))
            .order_by(table.c.created)
            .limit(limit)
        )
        rows = await self._run(lambda session: session.execute(query).mappings().all())
        found = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except ValueError:
                payload = None
            message = OutboxMessage(
                row["id"],
                row["topic"],
                payload,
                row["attempts"],
                row["created"],
                row.get("traceparent"),
            )
            found.append(message)
        return found

    async def backlog(self) -> tuple[int, float | None]:
        """How many messages aren't sent yet, and when the oldest of them was written (None for
        none): one query."""
        table = self.table
        query = select(func.count(), func.min(table.c.created)).where(table.c.sent.is_(None))
        count, oldest = await self._run(lambda session: session.execute(query).one())
        return int(count), oldest

    async def measure(self) -> None:
        """Set the gauges of its backlog (`outbox.backlog`, `outbox.oldest_age`): what a
        `BacklogMonitor` calls."""
        count, oldest = await self.backlog()
        metrics, _ = _observers()
        age = max(0.0, time.time() - oldest) if oldest is not None else 0.0
        metrics.set("outbox.backlog", count, outbox=self.table.name)
        metrics.set("outbox.oldest_age", age, outbox=self.table.name)

    async def sent(self, message_id: str) -> None:
        await self._update(message_id, sent=time.time())

    async def retry(self, message_id: str, attempts: int, at: float, error: str) -> None:
        await self._update(message_id, attempts=attempts, next_try=at, error=error[:500])

    async def give_up(self, message_id: str, attempts: int, error: str) -> None:
        """No more tries: kept as sent, with `failed` and its last error, for someone to look."""
        now = time.time()
        await self._update(message_id, attempts=attempts, sent=now, failed=now, error=error[:500])

    async def purge(self, older_than: float, *, batch: int = 1000) -> int:
        """Delete the messages sent more than `older_than` seconds ago, `batch` at a time, each
        batch in a transaction of its own; returns how many. Those given up on stay."""
        if older_than < 0 or batch < 1:
            raise ValueError("older_than can't be negative, and batch is 1 or more")
        table = self.table
        cutoff = time.time() - older_than
        old = (
            select(table.c.id)
            .where(and_(table.c.sent < cutoff, table.c.failed.is_(None)))
            .limit(batch)
        )
        purged = 0
        while True:
            # The ids first: DELETE ... LIMIT isn't portable, nor a subquery of the same table
            # everywhere (MySQL).
            ids = await self._run(lambda session: session.execute(old).scalars().all())
            if ids:
                await self._delete(ids)
            purged += len(ids)
            if len(ids) < batch:
                return purged

    async def _delete(self, ids: list[str]) -> None:
        statement = delete(self.table).where(self.table.c.id.in_(ids))
        await self._run(lambda session: session.execute(statement), commit=True)

    async def given_up(self, limit: int = 100) -> list[GivenUpMessage]:
        """The messages the relay gave up on, the latest first; without their payload."""
        table = self.table
        query = (
            select(
                table.c.id,
                table.c.topic,
                table.c.attempts,
                table.c.created,
                table.c.failed,
                table.c.error,
            )
            .where(table.c.failed.is_not(None))
            .order_by(table.c.failed.desc())
            .limit(limit)
        )
        rows = await self._run(lambda session: session.execute(query).all())
        return [GivenUpMessage(*row) for row in rows]

    async def requeue(self, ids: Iterable[str] | None = None) -> int:
        """Deliver again messages the relay gave up on (all of them by default), now, with their
        attempts reset; returns how many. Messages that weren't given up on aren't touched."""
        table = self.table
        condition: ColumnElement[bool] = table.c.failed.is_not(None)
        if ids is not None:
            wanted = list(ids)
            if not wanted:
                return 0
            condition = and_(condition, table.c.id.in_(wanted))
        statement = (
            update(table)
            .where(condition)
            .values(attempts=0, next_try=time.time(), sent=None, failed=None, error=None)
        )
        result = await self._run(lambda session: session.execute(statement), commit=True)
        return int(result.rowcount)

    async def discard(self, ids: Iterable[str]) -> int:
        """Delete messages the relay gave up on; returns how many there were."""
        wanted = list(ids)
        if not wanted:
            return 0
        table = self.table
        statement = delete(table).where(and_(table.c.failed.is_not(None), table.c.id.in_(wanted)))
        result = await self._run(lambda session: session.execute(statement), commit=True)
        return int(result.rowcount)

    async def _update(self, message_id: str, **values: Any) -> None:
        statement = update(self.table).where(self.table.c.id == message_id).values(**values)
        await self._run(lambda session: session.execute(statement), commit=True)

    async def _run(self, work: Callable[[Session], Any], *, commit: bool = False) -> Any:
        """`work` in a session of `sessions`: an AsyncSession's run_sync, or a Session in a
        thread."""
        session = self.sessions()
        if AsyncSession is not None and isinstance(session, AsyncSession):
            async with session:
                result = await session.run_sync(work)
                if commit:
                    await session.commit()
                return result

        def blocking() -> Any:
            with session:
                result = work(session)
                if commit:
                    session.commit()
                return result

        return await run_in_threadpool(blocking)


class OutboxRelay:
    """Delivers the outbox's messages, the oldest first, at least once: `deliver(message)` is
    awaited, and the message marked as sent when it returns. When it raises, the message is
    tried again later (1, 2, 4… seconds, up to `max_delay`), and given up on after
    `max_attempts`: kept with its error, never sent, until `requeue` or `discard`. Run it in one
    process (`FastAPILeader`).

    Each delivery is a span (`fastapi_alcazaba.outbox <topic>`), the root of a trace of its own
    linked to the span that wrote the message; counted in `fastapi_alcazaba.outbox` (delivered,
    retried, given_up). Metrics and traces go to FastAPICache's providers when it's set up, to the
    global ones otherwise.

    :param outbox: where the messages are.
    :param deliver: `deliver(message)`, async: enqueue a task, call a webhook, publish.
    :param batch: messages read at a time.
    :param interval: seconds between two reads when there was nothing to deliver.
    :param max_attempts: deliveries tried before giving up on a message.
    :param max_delay: the longest wait between two tries, in seconds.
    :param retention: seconds the messages sent are kept; `run()` deletes them afterwards. None
        keeps them all.
    :param purge_interval: seconds between two purges in `run()`.
    :param purge_batch: messages deleted in each transaction of a purge.
    """

    def __init__(
        self,
        outbox: SQLAlchemyOutbox,
        deliver: Callable[[OutboxMessage], Awaitable[Any]],
        *,
        batch: int = 100,
        interval: float = 1.0,
        max_attempts: int = 10,
        max_delay: float = 3600,
        retention: float | None = 7 * 24 * 3600,
        purge_interval: float = 3600,
        purge_batch: int = 1000,
    ) -> None:
        if batch < 1 or max_attempts < 1 or interval <= 0 or max_delay < 1:
            raise ValueError("batch and max_attempts are 1 or more; interval and max_delay > 0")
        if (retention is not None and retention < 0) or purge_interval <= 0 or purge_batch < 1:
            raise ValueError("retention can't be negative; purge_interval > 0; purge_batch >= 1")
        self.outbox = outbox
        self.deliver = deliver
        self.batch = batch
        self.interval = interval
        self.max_attempts = max_attempts
        self.max_delay = max_delay
        self.retention = retention
        self.purge_interval = purge_interval
        self.purge_batch = purge_batch
        self._last_purge: float | None = None

    async def relay_once(self) -> int:
        """Deliver the messages due now; how many were sent."""
        metrics, tracer = _observers()
        name = self.outbox.table.name
        sent = 0
        for message in await self.outbox.due(self.batch):
            counted = {"outbox": name, "topic": message.topic}
            with tracer.outbox(
                name, message.topic, message.id, message.attempts, message.traceparent
            ) as span:
                try:
                    await self.deliver(message)
                except Exception as exc:  # the message is tried again: the relay goes on
                    record_failure(span, exc)
                    attempts = message.attempts + 1
                    error = f"{type(exc).__name__}: {exc}"
                    if attempts >= self.max_attempts:
                        logger.error("Gave up on outbox message %s (%s)", message.id, message.topic)
                        await self.outbox.give_up(message.id, attempts, error)
                        metrics.count("outbox", outcome="given_up", **counted)
                    else:
                        logger.warning("Outbox message %s not delivered: %s", message.id, error)
                        delay = min(self.max_delay, 2 ** (attempts - 1))
                        await self.outbox.retry(message.id, attempts, time.time() + delay, error)
                        metrics.count("outbox", outcome="retried", **counted)
                    continue
            await self.outbox.sent(message.id)
            metrics.count("outbox", outcome="delivered", **counted)
            sent += 1
        return sent

    async def purge(self) -> int:
        """Delete the messages sent more than `retention` ago; how many. `run()` calls it every
        `purge_interval`."""
        if self.retention is None:
            return 0
        purged = await self.outbox.purge(self.retention, batch=self.purge_batch)
        if purged:
            metrics, _ = _observers()
            metrics.add("outbox.purged", purged, outbox=self.outbox.table.name)
            logger.info("Purged %d messages of the outbox %r", purged, self.outbox.table.name)
        return purged

    async def run(self) -> None:
        """Relay until cancelled: a batch, and when there was nothing due, a pause; a purge every
        `purge_interval`."""
        while True:
            await self._purge_when_due()
            try:
                delivered = await self.relay_once()
            except Exception:  # the database is down: try again later
                logger.warning("Couldn't read the outbox", exc_info=True)
                delivered = 0
            if delivered < self.batch:
                await asyncio.sleep(self.interval)

    async def _purge_when_due(self) -> None:
        now = time.time()
        if self._last_purge is not None and now - self._last_purge < self.purge_interval:
            return
        self._last_purge = now
        try:
            await self.purge()
        except Exception:  # the database is down: the next purge will do
            logger.warning("Couldn't purge the outbox", exc_info=True)
