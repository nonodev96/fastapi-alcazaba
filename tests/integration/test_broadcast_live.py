"""FastAPIBroadcast against real servers, each enabled by its variable (REDIS_URL, VALKEY_URL):
two processes, each with its own client, publish and subscribe."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from fastapi_alcazaba.background import FastAPIBroadcast, Message

pytestmark = pytest.mark.integration
KEY = "broadcast-signing-key-of-32-bytes!"


@pytest.fixture(name="url", params=["REDIS_URL", "VALKEY_URL"])
def fixture_url(request: pytest.FixtureRequest) -> str:
    url = os.environ.get(request.param)
    if not url:
        pytest.skip(f"{request.param} not set")
    return url


def client(url: str) -> Any:
    if url.startswith("valkey"):
        import valkey.asyncio as valkey

        return valkey.from_url(url)  # type: ignore[no-untyped-call]
    import redis.asyncio as redis

    return redis.from_url(url)


@pytest.fixture(name="processes")
async def fixture_processes(
    url: str,
) -> AsyncIterator[tuple[FastAPIBroadcast, FastAPIBroadcast, Any]]:
    """Two processes' broadcasts, under a prefix of their own, and a third client."""
    prefix = f"alcazaba-test-{uuid.uuid4().hex}:"
    clients = [client(url) for _ in range(3)]
    first, second = (FastAPIBroadcast(c, signing_key=KEY, prefix=prefix) for c in clients[:2])
    yield first, second, clients[2]
    for broadcast in (first, second):
        await broadcast.close()
    for each in clients:
        await each.aclose()


async def next_message(messages: Any) -> Message:
    message: Message = await asyncio.wait_for(anext(messages), 5)
    return message


async def subscribed(broadcast: FastAPIBroadcast) -> None:
    """Until its connection to the server is subscribed."""
    for _ in range(100):
        if broadcast._pubsub is not None and broadcast._pubsub.channels:  # pylint: disable=protected-access
            return
        await asyncio.sleep(0.02)


async def test_from_one_process_to_another(processes: Any) -> None:
    first, second, _ = processes
    async with second.subscribe("room:7") as messages:
        await subscribed(second)
        await asyncio.sleep(0.1)
        await first.publish("room:7", {"text": "hi"})
        assert await next_message(messages) == Message("room:7", {"text": "hi"})


async def test_a_planted_message_is_dropped(processes: Any) -> None:
    first, second, raw = processes
    async with second.subscribe("room:7") as messages:
        await subscribed(second)
        await asyncio.sleep(0.1)
        planted = json.dumps({"data": json.dumps({"text": "planted"})})
        await raw.publish(f"{second.prefix}room:7", planted)  # no signature
        await first.publish("room:7", {"text": "signed"})
        assert (await next_message(messages)).data == {"text": "signed"}


async def test_a_lost_connection_is_a_gap(processes: Any) -> None:
    first, second, raw = processes
    async with second.subscribe("room:7") as messages:
        await subscribed(second)
        await raw.execute_command("CLIENT", "KILL", "TYPE", "pubsub")
        assert (await next_message(messages)).gap
        await subscribed(second)
        await asyncio.sleep(0.2)
        await first.publish("room:7", {"text": "after"})
        found = await next_message(messages)
        while found.gap:
            found = await next_message(messages)
        assert found.data == {"text": "after"}
