"""The outbox against PostgreSQL, through asyncpg and SQLAlchemy's async sessions, enabled by

    POSTGRES_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/postgres

Each test has a table of its own, dropped at its end, so it's safe on a shared database.
"""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fastapi_alcazaba.background.outbox import (
    OutboxMessage,
    OutboxRelay,
    SQLAlchemyOutbox,
    outbox_table,
)

pytestmark = pytest.mark.integration


@pytest.fixture(name="outbox")
async def fixture_outbox() -> AsyncIterator[SQLAlchemyOutbox]:
    url = os.environ.get("POSTGRES_URL")
    if not url:
        pytest.skip("POSTGRES_URL not set")
    engine = create_async_engine(url)
    metadata = MetaData()
    table = outbox_table(metadata, f"outbox_{uuid.uuid4().hex[:12]}")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        yield SQLAlchemyOutbox(table, async_sessionmaker(engine))
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


async def test_written_relayed_given_up_requeued_and_purged(outbox: SQLAlchemyOutbox) -> None:
    sessions = outbox.sessions
    async with sessions() as session, session.begin():
        kept = await outbox.add(session, "order.placed", {"order": 1})
        failing = await outbox.add(session, "order.placed", {"order": 2})
    async with sessions() as session, session.begin():
        await outbox.add(session, "order.placed", {"order": 3})
        await session.rollback()  # the change rolled back: no message either
    delivered: list[OutboxMessage] = []

    async def deliver(message: OutboxMessage) -> None:
        if message.id == failing:
            raise ConnectionError("the queue is down")
        delivered.append(message)

    relay = OutboxRelay(outbox, deliver, max_attempts=1)
    assert await relay.relay_once() == 1
    assert [message.payload for message in delivered] == [{"order": 1}]
    assert delivered[0].id == kept
    (given_up,) = await outbox.given_up()
    assert (given_up.id, given_up.attempts) == (failing, 1)
    assert await outbox.backlog() == (0, None)
    assert await outbox.requeue() == 1
    count, oldest = await outbox.backlog()
    assert count == 1
    assert oldest is not None
    assert await outbox.purge(0) == 1  # the one sent; the requeued one isn't sent
    assert await outbox.discard([failing]) == 0  # requeued: not given up on any more


async def test_the_relay_s_query_has_its_index(outbox: SQLAlchemyOutbox) -> None:
    """With sequential scans off, the planner can serve the relay's query from `due`: the index
    fits it (on a table this small it would scan, which says nothing)."""
    table = outbox.table.name
    async with outbox.sessions() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        query = (
            f"EXPLAIN SELECT * FROM {table} "  # noqa: S608  # the test's own table name
            "WHERE sent IS NULL AND next_try <= 1e12 ORDER BY created LIMIT 100"
        )
        plan = (await session.execute(text(query))).scalars()
        assert f"ix_{table}_due" in "\n".join(plan)
