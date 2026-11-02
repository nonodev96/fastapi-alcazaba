"""FastAPIBroadcast in one process (no client): channels, subscribers, bounds and gaps. Against
Redis and Valkey: tests/integration/test_backends_live.py."""

import asyncio
from typing import Any

import pytest

from fastapi_alcazaba.background import FastAPIBroadcast, Message


async def take(messages: Any, count: int) -> list[Message]:
    return [await asyncio.wait_for(anext(messages), 1) for _ in range(count)]


async def test_subscribers_of_a_channel_get_its_messages() -> None:
    broadcast = FastAPIBroadcast()
    async with broadcast.subscribe("room:7") as first, broadcast.subscribe("room:7") as second:
        async with broadcast.subscribe("room:8") as other:
            await broadcast.publish("room:7", {"text": "hi"})
            await broadcast.publish("room:8", {"text": "elsewhere"})
            assert await take(first, 1) == [Message("room:7", {"text": "hi"})]
            assert await take(second, 1) == [Message("room:7", {"text": "hi"})]
            assert await take(other, 1) == [Message("room:8", {"text": "elsewhere"})]


async def test_messages_for_nobody_are_dropped() -> None:
    broadcast = FastAPIBroadcast()
    await broadcast.publish("room:7", "nobody listens")
    async with broadcast.subscribe("room:7") as messages:
        await broadcast.publish("room:7", "now someone does")
        assert (await take(messages, 1))[0].data == "now someone does"


async def test_a_subscriber_that_falls_behind_gets_a_gap() -> None:
    broadcast = FastAPIBroadcast(queue_size=3)
    async with broadcast.subscribe("feed") as messages:
        for number in range(5):
            await broadcast.publish("feed", number)
        found = await take(messages, 3)
        assert any(message.gap for message in found)
        assert found[-1].data == 4  # the newest is kept


@pytest.mark.parametrize(
    ("channel", "data", "error"),
    [
        ("", 1, ValueError),
        ("room 7", 1, ValueError),
        ("x" * 201, 1, ValueError),
        ("room:7", float("nan"), ValueError),
        ("room:7", "x" * 70_000, ValueError),
        ("room:7", object(), TypeError),
    ],
)
async def test_what_isnt_published(channel: str, data: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        await FastAPIBroadcast().publish(channel, data)


def test_configuration() -> None:
    with pytest.raises(TypeError, match="pubsub"):
        FastAPIBroadcast(object())
    with pytest.raises(ValueError, match="queue_size"):
        FastAPIBroadcast(queue_size=1)
