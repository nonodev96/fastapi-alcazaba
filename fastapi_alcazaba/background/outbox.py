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
"""

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from starlette.concurrency import run_in_threadpool

try:
    from sqlalchemy import (
        Column,
        Float,
        Integer,
        MetaData,
        String,
        Table,
        Text,
        and_,
        select,
        update,
    )
    from sqlalchemy.orm import Session
except ImportError as exc:  # pragma: no cover  # the extra isn't installed
    raise ImportError(
        'the outbox needs SQLAlchemy: pip install "fastapi-alcazaba[sqlalchemy]"'
    ) from exc
try:
    from sqlalchemy.ext.asyncio import AsyncSession
except ImportError:  # greenlet isn't installed: no async sessions, then
    AsyncSession = None  # type: ignore[assignment,misc]

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: The longest payload, as JSON.
MAX_PAYLOAD = 256 * 1024


def outbox_table(metadata: MetaData, name: str = "outbox") -> Table:
    """The outbox's table, in your metadata (its migrations create it): each message's id, topic,
    payload (JSON), when it was written, the attempts at delivering it, when it may be tried
    again, and when it was sent (or given up on)."""
    return Table(
        name,
        metadata,
        Column("id", String(32), primary_key=True),
        Column("topic", String(200), nullable=False),
        Column("payload", Text, nullable=False),
        Column("created", Float, nullable=False),
        Column("attempts", Integer, nullable=False, default=0),
        Column("next_try", Float, nullable=False, index=True),
        Column("sent", Float, nullable=True, index=True),
        Column("failed", Float, nullable=True),
        Column("error", String(500), nullable=True),
    )


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """A message of the outbox, as `deliver` gets it.

    :param id: random, unique: what consumers deduplicate by.
    :param topic: what it's about, such as "order.placed".
    :param payload: what was written with it.
    :param attempts: the deliveries tried before this one.
    :param created: when it was written (Unix time).
    """

    id: str
    topic: str
    payload: Any
    attempts: int
    created: float


#: A session of SQLAlchemy, plain or async.
AnySession = Any


class SQLAlchemyOutbox:
    """The outbox in a table of your database (`outbox_table`), written through your sessions.

    :param table: the outbox's table.
    :param sessions: makes the relay's sessions (a `sessionmaker` or an `async_sessionmaker`).
    """

    def __init__(self, table: Table, sessions: Callable[[], AnySession]) -> None:
        self.table = table
        self.sessions = sessions

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
        statement = self.table.insert().values(
            id=message_id, topic=topic, payload=body, created=now, attempts=0, next_try=now
        )
        result = session.execute(statement)
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
                row["id"], row["topic"], payload, row["attempts"], row["created"]
            )
            found.append(message)
        return found

    async def sent(self, message_id: str) -> None:
        await self._update(message_id, sent=time.time())

    async def retry(self, message_id: str, attempts: int, at: float, error: str) -> None:
        await self._update(message_id, attempts=attempts, next_try=at, error=error[:500])

    async def give_up(self, message_id: str, attempts: int, error: str) -> None:
        """No more tries: kept as sent, with `failed` and its last error, for someone to look."""
        now = time.time()
        await self._update(message_id, attempts=attempts, sent=now, failed=now, error=error[:500])

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
    `max_attempts`: kept with its error, never sent. Run it in one process (`FastAPILeader`).

    :param outbox: where the messages are.
    :param deliver: `deliver(message)`, async: enqueue a task, call a webhook, publish.
    :param batch: messages read at a time.
    :param interval: seconds between two reads when there was nothing to deliver.
    :param max_attempts: deliveries tried before giving up on a message.
    :param max_delay: the longest wait between two tries, in seconds.
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
    ) -> None:
        if batch < 1 or max_attempts < 1 or interval <= 0 or max_delay < 1:
            raise ValueError("batch and max_attempts are 1 or more; interval and max_delay > 0")
        self.outbox = outbox
        self.deliver = deliver
        self.batch = batch
        self.interval = interval
        self.max_attempts = max_attempts
        self.max_delay = max_delay

    async def relay_once(self) -> int:
        """Deliver the messages due now; how many were sent."""
        sent = 0
        for message in await self.outbox.due(self.batch):
            try:
                await self.deliver(message)
            except Exception as exc:  # the message is tried again: the relay goes on
                attempts = message.attempts + 1
                error = f"{type(exc).__name__}: {exc}"
                if attempts >= self.max_attempts:
                    logger.error("Gave up on outbox message %s (%s)", message.id, message.topic)
                    await self.outbox.give_up(message.id, attempts, error)
                else:
                    logger.warning("Outbox message %s not delivered: %s", message.id, error)
                    delay = min(self.max_delay, 2 ** (attempts - 1))
                    await self.outbox.retry(message.id, attempts, time.time() + delay, error)
                continue
            await self.outbox.sent(message.id)
            sent += 1
        return sent

    async def run(self) -> None:
        """Relay until cancelled: a batch, and when there was nothing due, a pause."""
        while True:
            try:
                delivered = await self.relay_once()
            except Exception:  # the database is down: try again later
                logger.warning("Couldn't read the outbox", exc_info=True)
                delivered = 0
            if delivered < self.batch:
                await asyncio.sleep(self.interval)
