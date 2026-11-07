"""WebhookSender: deliveries through the task queue to a fake receiver, signed, retried, recorded,
and endpoints that keep failing disabled; the SSRF guard of its own client."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx2 as httpx
import pytest

from fastapi_alcazaba.background import FastAPITaskQueue
from fastapi_alcazaba.outbound.transports import ResilientTransport, SSRFGuardTransport
from fastapi_alcazaba.webhooks import (
    FastAPIWebhooks,
    WebhookDeliveryFailed,
    WebhookEndpoint,
    WebhookSender,
    WebhookSigner,
)
from fastapi_alcazaba.webhooks.sender import TASK
from tests.unit.test_tasks import World, eventually, settled

SECRET = WebhookSigner.generate_secret()


class Receiver:
    """Stands in for the endpoints: answers each POST with the next status of `statuses`
    (200 when they run out), and keeps what it got."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.received: list[httpx.Request] = []
        self.verifier = FastAPIWebhooks(SECRET)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.received.append(request)
        status = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(status, text="secret-free answer")

    def messages(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.received]


class Setup:
    def __init__(self, receiver: Receiver, **options: Any) -> None:
        self.world = World()
        self.queue: FastAPITaskQueue = self.world.queue()
        self.endpoints: dict[str, WebhookEndpoint] = {
            "ep_1": WebhookEndpoint("https://hooks.example.com/a", SECRET),
            "ep_2": WebhookEndpoint("https://hooks.example.com/b", SECRET),
        }
        options.setdefault("backoff", 0.01)
        options.setdefault("max_backoff", 0.02)
        client = httpx.AsyncClient(transport=httpx.MockTransport(receiver.handler))
        self.sender = WebhookSender(self.queue, self.lookup, client=client, **options)

    async def lookup(self, endpoint_id: str) -> WebhookEndpoint | None:
        return self.endpoints.get(endpoint_id)


async def until(condition: Callable[[], Any]) -> None:
    await eventually(condition, timeout=5)


class TestDelivering:
    async def test_signed_with_the_same_id_at_every_attempt(self) -> None:
        receiver = Receiver(500, 503)
        setup = Setup(receiver)
        async with setup.queue:
            message_id = await setup.sender.send("ep_1", {"type": "order.paid", "id": 7})
            await until(lambda: len(receiver.received) == 3)
            await until(lambda: settled(setup.queue))
        ids = {request.headers["webhook-id"] for request in receiver.received}
        assert ids == {message_id}
        for request in receiver.received:
            assert request.headers["content-type"] == "application/json"
            assert str(request.url) == "https://hooks.example.com/a"
            assert receiver.verifier.verify(request.headers, request.content).id == message_id
        assert receiver.messages()[0] == {"type": "order.paid", "id": 7}
        delivery = await setup.sender.delivery("ep_1", message_id)
        assert delivery is not None
        assert (delivery.state, delivery.attempts, delivery.status_code, delivery.error) == (
            "succeeded",
            3,
            200,
            None,
        )
        assert (await setup.sender.health("ep_1")).failures == 0  # reset by the success

    async def test_the_same_message_twice_is_delivered_once(self) -> None:
        receiver = Receiver()
        setup = Setup(receiver)
        for _ in range(3):  # the outbox relayed it again, the app retried
            await setup.sender.send("ep_1", {"n": 1}, message_id="msg_outbox_1")
        await setup.sender.send("ep_2", {"n": 1}, message_id="msg_outbox_1")  # another endpoint
        async with setup.queue:
            await until(lambda: settled(setup.queue))
        assert sorted(str(request.url)[-1] for request in receiver.received) == ["a", "b"]

    async def test_retries_spent_go_to_the_dead_letters(self) -> None:
        receiver = Receiver(*[500] * 10)
        setup = Setup(receiver, retries=2)
        async with setup.queue:
            message_id = await setup.sender.send("ep_1", {"n": 1})
            await until(setup.queue.dead_letters)
        assert len(receiver.received) == 3
        delivery = await setup.sender.delivery("ep_1", message_id)
        assert delivery is not None
        assert (delivery.state, delivery.attempts, delivery.error) == ("failed", 3, "http_500")
        (dead,) = await setup.queue.dead_letters()
        assert (dead.task, dead.error) == (
            TASK,
            "fastapi_alcazaba.webhooks.sender.WebhookDeliveryFailed",
        )

    async def test_a_transport_error_and_an_endpoint_gone(self) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        setup = Setup(Receiver(), retries=0)
        setup.sender.client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
        async with setup.queue:
            failing = await setup.sender.send("ep_1", {"n": 1})
            gone = await setup.sender.send("ep_unknown", {"n": 2})
            await until(lambda: settled(setup.queue))
        delivery = await setup.sender.delivery("ep_1", failing)
        assert delivery is not None
        assert (delivery.status_code, delivery.error) == (None, "ConnectError")
        skipped = await setup.sender.delivery("ep_unknown", gone)
        assert skipped is not None
        assert (skipped.state, skipped.error) == ("skipped", "endpoint_gone")

    async def test_a_sync_lookup(self) -> None:
        receiver = Receiver()
        setup = Setup(receiver)
        setup.sender.endpoints = setup.endpoints.get
        async with setup.queue:
            await setup.sender.send("ep_2", [1, 2])
            await until(lambda: receiver.received)

    async def test_no_secret_nor_body_in_the_backend(self) -> None:
        setup = Setup(Receiver())
        message_id = await setup.sender.send("ep_1", {"card": "4111-1111"})
        assert await setup.sender.delivery("ep_1", message_id) is None  # not attempted yet
        async with setup.queue:
            await until(lambda: settled(setup.queue))
        dumped = b"".join(
            [await setup.world.raw.dump(key) or b"" async for key in setup.world.raw.scan_iter()]
        )
        assert SECRET.removeprefix("whsec_").encode() not in dumped


class TestEndpoints:
    async def test_disabled_after_failures_in_a_row_and_enabled_again(self) -> None:
        receiver = Receiver(*[500] * 100)
        setup = Setup(receiver, retries=10, disable_after=3)
        async with setup.queue:
            message_id = await setup.sender.send("ep_1", {"n": 1})
            await until(lambda: settled(setup.queue))
            assert len(receiver.received) == 3
            health = await setup.sender.health("ep_1")
            assert (health.failures, health.disabled) == (3, True)
            delivery = await setup.sender.delivery("ep_1", message_id)
            assert delivery is not None
            assert delivery.state == "failed"
            skipped = await setup.sender.send("ep_1", {"n": 2})  # disabled: not sent
            await until(lambda: settled(setup.queue))
            record = await setup.sender.delivery("ep_1", skipped)
            assert record is not None
            assert (record.state, record.error) == ("skipped", "endpoint_disabled")
            assert len(receiver.received) == 3
            await setup.sender.enable("ep_1")
            assert await setup.sender.health("ep_1") == type(health)("ep_1", 0, False)
            receiver.statuses.clear()
            await setup.sender.send("ep_1", {"n": 3})
            await until(lambda: len(receiver.received) == 4)

    async def test_410_disables_at_once(self) -> None:
        receiver = Receiver(410)
        setup = Setup(receiver)
        async with setup.queue:
            await setup.sender.send("ep_2", {"n": 1})
            await until(lambda: settled(setup.queue))
        assert (await setup.sender.health("ep_2")).disabled
        assert len(receiver.received) == 1

    async def test_disable_by_hand(self) -> None:
        setup = Setup(Receiver())
        await setup.sender.disable("ep_1")
        assert (await setup.sender.health("ep_1")).disabled


class TestDefaults:
    async def test_its_client_refuses_private_addresses(self) -> None:
        world = World()
        queue = world.queue()
        endpoint = WebhookEndpoint("http://127.0.0.1:9/hook", SECRET)
        sender = WebhookSender(queue, lambda _: endpoint, retries=0)
        async with queue:
            message_id = await sender.send("ep", {"n": 1})
            await until(lambda: settled(queue))
        delivery = await sender.delivery("ep", message_id)
        assert delivery is not None
        assert delivery.error == "BlockedDestinationError"
        await sender.aclose()
        assert sender.client.is_closed

    async def test_one_endpoints_failures_dont_block_anothers_on_its_host(self) -> None:
        """Its own client has no breaker per host: a user's endpoint that keeps failing neither
        keeps another user's on the same host from being called, nor counts against it."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500 if request.url.path == "/failing" else 200)

        endpoints = {
            "failing": WebhookEndpoint("https://93.184.215.14/failing", SECRET),
            "healthy": WebhookEndpoint("https://93.184.215.14/healthy", SECRET),
        }
        world = World()
        sender = WebhookSender(world.queue(), endpoints.get, retries=0)
        resilient = sender.client._transport
        assert isinstance(resilient, ResilientTransport)
        guard = resilient._transport
        assert isinstance(guard, SSRFGuardTransport)
        guard._shared = httpx.MockTransport(handler)  # what the guard sends through
        for n in range(10):
            with pytest.raises(WebhookDeliveryFailed):
                await sender._deliver("failing", f"m{n}", "{}")
        await sender._deliver("healthy", "m", "{}")
        delivery = await sender.delivery("healthy", "m")
        assert delivery is not None
        assert delivery.state == "succeeded"
        assert (await sender.health("healthy")).failures == 0
        await sender.aclose()

    async def test_a_client_given_is_not_closed(self) -> None:
        client = httpx.AsyncClient()
        sender = WebhookSender(World().queue(), lambda _: None, client=client)
        await sender.aclose()
        assert not client.is_closed
        await client.aclose()

    async def test_records_someone_else_wrote(self) -> None:
        setup = Setup(Receiver())
        key = setup.sender._key("delivery", "ep_1", "m")
        await setup.world.raw.set(key, b"{not json")
        assert await setup.sender.delivery("ep_1", "m") is None

    @pytest.mark.parametrize(
        "options",
        [{"timeout": 0}, {"record_ttl": 0}, {"disable_after": 0}],
    )
    def test_invalid(self, options: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="timeout"):
            WebhookSender(World().queue(), lambda _: None, **options)

    async def test_invalid_sends(self) -> None:
        setup = Setup(Receiver())
        with pytest.raises(ValueError, match="endpoint_id"):
            await setup.sender.send("", {})
        with pytest.raises(ValueError, match="JSON compliant"):
            await setup.sender.send("ep_1", float("nan"))

    def test_one_sender_per_queue(self) -> None:
        queue = World().queue()
        WebhookSender(queue, lambda _: None)
        with pytest.raises(ValueError, match="already a task"):
            WebhookSender(queue, lambda _: None)


async def test_concurrent_attempts_dont_lose_failures() -> None:
    receiver = Receiver(*[500] * 6)
    setup = Setup(receiver, retries=0, disable_after=100)
    async with setup.queue:
        await asyncio.gather(*(setup.sender.send("ep_1", {"n": n}) for n in range(6)))
        await until(lambda: settled(setup.queue))
    assert (await setup.sender.health("ep_1")).failures == 6


async def test_outside_a_task_it_counts_one_attempt() -> None:
    """Called directly (a test, a script), a delivery is its first attempt of `retries + 1`."""
    setup = Setup(Receiver(500), retries=3)
    with pytest.raises(WebhookDeliveryFailed, match="http_500"):
        await setup.sender._deliver("ep_1", "msg_x", "{}")
    delivery = await setup.sender.delivery("ep_1", "msg_x")
    assert delivery is not None
    assert (delivery.state, delivery.attempts) == ("retrying", 1)
    await setup.sender._record("ep_1", "msg_y", "skipped")
    skipped = await setup.sender.delivery("ep_1", "msg_y")
    assert skipped is not None
    assert skipped.attempts == 0


def test_fastapicache_when_neither_has_a_cache() -> None:
    from fastapi_alcazaba import FastAPICache
    from fastapi_alcazaba.backends.inmemory import InMemoryBackend

    FastAPICache.init(InMemoryBackend())
    sender = WebhookSender(FastAPITaskQueue(), lambda _: None)
    assert sender._cache() is FastAPICache.instance()
