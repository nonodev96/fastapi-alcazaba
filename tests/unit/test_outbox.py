"""The transactional outbox: a message only with its transaction, delivered at least once, tried
again later, and given up on after max_attempts."""

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fastapi_alcazaba.background import outbox as outbox_module
from fastapi_alcazaba.background.outbox import (
    OutboxMessage,
    OutboxRelay,
    SQLAlchemyOutbox,
    outbox_table,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture(name="clock", autouse=True)
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(outbox_module, "time", clock)
    return clock


class Shop:
    """A database with orders and the outbox, and what the relay delivered."""

    def __init__(self) -> None:
        # One in-memory database, shared by the relay's thread and the test's.
        self.engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        metadata = MetaData()
        self.orders = Table(
            "orders", metadata, Column("id", Integer, primary_key=True), Column("item", String(50))
        )
        self.table = outbox_table(metadata)
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine)
        self.outbox = SQLAlchemyOutbox(self.table, self.sessions)
        self.delivered: list[OutboxMessage] = []
        self.failing = 0  # deliveries that raise before they work

    async def deliver(self, message: OutboxMessage) -> None:
        if self.failing:
            self.failing -= 1
            raise ConnectionError("the queue is down")
        self.delivered.append(message)

    async def place(self, item: str, *, fail: bool = False) -> str:
        with self.sessions() as session, session.begin():
            session.execute(self.orders.insert().values(item=item))
            message_id = await self.outbox.add(session, "order.placed", {"item": item})
            if fail:
                raise RuntimeError("the order fails after the message was written")
        return message_id

    def rows(self) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(self.table)).mappings()]

    def close(self) -> None:
        self.engine.dispose()


@pytest.fixture(name="shop")
def fixture_shop() -> Iterator[Shop]:
    shop = Shop()
    yield shop
    shop.close()


async def test_a_message_with_its_change(shop: Shop) -> None:
    message_id = await shop.place("olive oil")
    relay = OutboxRelay(shop.outbox, shop.deliver)
    assert await relay.relay_once() == 1
    assert [(m.id, m.topic, m.payload) for m in shop.delivered] == [
        (message_id, "order.placed", {"item": "olive oil"})
    ]
    assert shop.rows()[0]["sent"] is not None
    assert await relay.relay_once() == 0  # sent once


async def test_no_message_without_its_change(shop: Shop) -> None:
    with pytest.raises(RuntimeError):
        await shop.place("olives", fail=True)
    assert shop.rows() == []
    assert await OutboxRelay(shop.outbox, shop.deliver).relay_once() == 0


async def test_tried_again_later(shop: Shop, clock: Clock) -> None:
    await shop.place("olive oil")
    shop.failing = 2
    relay = OutboxRelay(shop.outbox, shop.deliver)
    assert await relay.relay_once() == 0
    assert await relay.relay_once() == 0  # not due yet: in a second
    clock.now += 1
    assert await relay.relay_once() == 0  # the second failure: in two seconds
    clock.now += 2
    assert await relay.relay_once() == 1
    assert shop.delivered[0].attempts == 2
    assert "ConnectionError" in shop.rows()[0]["error"]  # the last one, kept


async def test_given_up_on(shop: Shop, clock: Clock) -> None:
    await shop.place("olive oil")
    shop.failing = 100
    relay = OutboxRelay(shop.outbox, shop.deliver, max_attempts=3)
    for _ in range(3):
        await relay.relay_once()
        clock.now += 10
    row = shop.rows()[0]
    assert (row["attempts"], row["failed"] is not None) == (3, True)
    assert "the queue is down" in row["error"]
    clock.now += 10_000
    assert await relay.relay_once() == 0


async def test_the_oldest_first_by_batches(shop: Shop, clock: Clock) -> None:
    for item in ("a", "b", "c"):
        await shop.place(item)
        clock.now += 1
    relay = OutboxRelay(shop.outbox, shop.deliver, batch=2)
    assert await relay.relay_once() == 2
    assert await relay.relay_once() == 1
    assert [m.payload["item"] for m in shop.delivered] == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("topic", "payload", "error"),
    [
        ("", {}, ValueError),
        ("t" * 201, {}, ValueError),
        ("x", float("nan"), ValueError),
        ("x", object(), TypeError),
    ],
)
async def test_what_isnt_written(
    shop: Shop,
    topic: str,
    payload: Any,
    error: type[Exception],
) -> None:
    with shop.sessions() as session, pytest.raises(error):
        await shop.outbox.add(session, topic, payload)


def test_configuration(shop: Shop) -> None:
    with pytest.raises(ValueError, match="batch"):
        OutboxRelay(shop.outbox, shop.deliver, batch=0)
