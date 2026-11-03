"""WebhookSender through the task queue on real servers (as test_tasks_live, each enabled by its
environment variable): two processes deliver, each message once per endpoint, retried until the
receiver takes it, and an endpoint that keeps failing disabled."""

import httpx2 as httpx

from fastapi_alcazaba.webhooks import FastAPIWebhooks, WebhookEndpoint, WebhookSender, WebhookSigner
from tests.integration.test_tasks_live import (
    KINDS,
    Processes,
    eventually,
    fixture_processes,
    settled,
)

__all__ = ["KINDS", "fixture_processes"]  # the `processes` fixture, from the task queue's tests

SECRET = WebhookSigner.generate_secret()


async def test_delivered_once_retried_and_disabled(processes: Processes) -> None:
    received: list[httpx.Request] = []
    failing = {"https://hooks.example.com/flaky": 2, "https://hooks.example.com/down": 100}

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        url = str(request.url)
        if failing.get(url, 0) > 0:
            failing[url] -= 1
            return httpx.Response(503)
        return httpx.Response(204)

    endpoints = {
        "ok": WebhookEndpoint("https://hooks.example.com/ok", SECRET),
        "flaky": WebhookEndpoint("https://hooks.example.com/flaky", SECRET),
        "down": WebhookEndpoint("https://hooks.example.com/down", SECRET),
    }
    queues = [processes.queue(), processes.queue()]
    senders = [
        WebhookSender(
            queue,
            endpoints.get,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            backoff=0.05,
            max_backoff=0.1,
            disable_after=3,
        )
        for queue in queues
    ]
    for queue in queues:
        await queue.start()
    for endpoint_id in endpoints:
        for sender in senders:  # both processes send it: delivered once
            await sender.send(endpoint_id, {"event": "order.paid"}, message_id="msg_1")
    await eventually(lambda: settled(*queues), timeout=20)
    by_url: dict[str, int] = {}
    for request in received:
        by_url[str(request.url)] = by_url.get(str(request.url), 0) + 1
        assert FastAPIWebhooks(SECRET).verify(request.headers, request.content).id == "msg_1"
    assert by_url == {
        "https://hooks.example.com/ok": 1,
        "https://hooks.example.com/flaky": 3,
        "https://hooks.example.com/down": 3,
    }
    assert (await senders[1].health("down")).disabled
    delivery = await senders[1].delivery("flaky", "msg_1")
    assert delivery is not None
    assert (delivery.state, delivery.attempts) == ("succeeded", 3)
