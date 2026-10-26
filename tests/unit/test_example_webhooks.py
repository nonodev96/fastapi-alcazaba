"""The webhooks example: signed webhooks taken once, and sent only to the public internet. Nothing
goes to the network: DNS answers from a table, and the users' servers are a MockTransport."""

import asyncio
import json
import socket
import time
from collections.abc import Iterator
from typing import Any

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from examples.webhooks.main import Shop, create_app, settings
from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.webhooks import FastAPIWebhooks, WebhookMessage, WebhookSigner

PUBLIC = "93.184.215.14"
provider = WebhookSigner(settings.payments_webhook_secret)


class DNS:
    """Stands in for the event loop's getaddrinfo, from a table of records."""

    def __init__(self) -> None:
        self.records = {
            "hooks.example.com": [PUBLIC],
            "other.example.org": ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
            "localhost": ["127.0.0.1", "::1"],
            "intranet.example.com": ["10.0.0.7"],
        }

    def answer(self, host: str, port: int) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        if host not in self.records:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port),
            )
            for address in self.records[host]
        ]


class Receiver:
    """The users' servers: what reaches them, past the guard, and what they answer."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 204
        self.error: httpx.HTTPError | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.error is not None:
            raise self.error
        self.requests.append(request)
        return httpx.Response(self.status)

    def events(self, host: str = "hooks.example.com") -> list[str]:
        return [json.loads(r.content)["type"] for r in self.requests if r.headers["host"] == host]


@pytest.fixture(name="dns")
def fixture_dns(monkeypatch: pytest.MonkeyPatch) -> DNS:
    dns = DNS()

    async def getaddrinfo(
        _loop: object,
        host: str,
        port: int,
        **_: object,
    ) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        return dns.answer(host, port)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)
    return dns


@pytest.fixture(name="receiver")
def fixture_receiver() -> Receiver:
    return Receiver()


@pytest.fixture(name="client")
# pylint: disable-next=unused-argument  # requested for its effect: no real DNS
def fixture_client(dns: DNS, receiver: Receiver) -> Iterator[TestClient]:
    with TestClient(create_app(httpx.MockTransport(receiver))) as client:
        yield client
    FastAPICache.reset()


def shop(client: TestClient) -> Shop:
    found: Shop = client.app.state.shop  # type: ignore[attr-defined]
    return found


def event(order_id: str = "ord_1", amount: int = 1999, kind: str = "payment.succeeded") -> bytes:
    return json.dumps({"type": kind, "data": {"order_id": order_id, "amount": amount}}).encode()


def send(client: TestClient, body: bytes, headers: dict[str, str]) -> httpx.Response:
    return client.post("/webhooks/payments", content=body, headers=headers)


def pay(client: TestClient, body: bytes | None = None, **options: Any) -> httpx.Response:
    """A payment event, signed by the provider."""
    body = event() if body is None else body
    return send(client, body, provider.sign(body, **options))


def register(client: TestClient, url: str = "https://hooks.example.com/shop") -> httpx.Response:
    return client.post("/endpoints", json={"url": url})


class TestReceiving:
    def test_a_signed_event(self, client: TestClient) -> None:
        response = pay(client, event("ord_7", 2500))
        assert response.status_code == 204
        assert shop(client).paid == {"ord_7": 2500}

    def test_without_signature(self, client: TestClient) -> None:
        response = client.post("/webhooks/payments", content=event())
        assert (response.status_code, response.json()) == (400, {"detail": "Invalid webhook"})
        assert shop(client).paid == {}

    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param(WebhookSigner(WebhookSigner.generate_secret()).sign(event()), id="secret"),
            pytest.param(provider.sign(event("ord_1", 1)), id="another body"),
            pytest.param(provider.sign(event(), timestamp=time.time() - 600), id="too old"),
        ],
    )
    def test_a_bad_signature(self, client: TestClient, headers: dict[str, str]) -> None:
        response = send(client, event(), headers)
        assert (response.status_code, response.json()) == (401, {"detail": "Invalid webhook"})
        assert shop(client).paid == {}

    def test_a_replay_is_refused(self, client: TestClient) -> None:
        body = event()
        headers = provider.sign(body)
        assert send(client, body, headers).status_code == 204
        assert send(client, body, headers).status_code == 401

    def test_a_retry_is_handled_once(self, client: TestClient, receiver: Receiver) -> None:
        register(client)
        first = pay(client, message_id="msg_1", timestamp=time.time() - 30)
        retry = pay(client, message_id="msg_1")  # a new timestamp and signature
        assert (first.status_code, retry.status_code) == (204, 204)
        assert receiver.events() == ["ping", "order.paid"]

    def test_the_body_is_parsed_once_signed(self, client: TestClient) -> None:
        response = pay(client, b'{"type": "payment.succeeded"}')
        assert (response.status_code, response.json()) == (422, {"detail": "Not a payment event"})
        assert send(client, b"{not json", {}).status_code == 400  # not a 422: unsigned

    def test_other_events_are_acknowledged(self, client: TestClient, receiver: Receiver) -> None:
        register(client)
        assert pay(client, event(kind="payment.failed")).status_code == 204
        assert shop(client).paid == {}
        assert receiver.events() == ["ping"]


class TestRegistering:
    def test_a_public_url_gets_a_signed_ping(self, client: TestClient, receiver: Receiver) -> None:
        response = register(client)
        assert response.status_code == 201
        created = response.json()
        assert created["url"] == "https://hooks.example.com/shop"
        [ping] = receiver.requests
        # Sent to the address checked, with the name for Host (and TLS).
        assert (str(ping.url), ping.headers["host"]) == (
            f"https://{PUBLIC}/shop",
            "hooks.example.com",
        )
        message = FastAPIWebhooks(created["secret"]).verify(ping.headers, ping.content)
        assert isinstance(message, WebhookMessage)
        assert message.json() == {"id": message.id, "type": "ping"}

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",  # cloud metadata
            "http://localhost/admin",
            "http://[::1]/",
            "http://10.0.0.7/",
            "https://intranet.example.com/",  # a name for 10.0.0.7
            "http://2130706433/",  # 127.0.0.1
            "http://[::ffff:127.0.0.1]/",
        ],
    )
    def test_private_addresses_are_refused(
        self,
        client: TestClient,
        receiver: Receiver,
        url: str,
    ) -> None:
        response = register(client, url)
        assert response.status_code == 422
        assert response.json()["detail"].endswith(": its address isn't allowed")
        assert receiver.requests == []
        assert shop(client).endpoints == {}

    def test_the_refusal_says_which_host(self, client: TestClient) -> None:
        response = register(client, "http://169.254.169.254/latest/meta-data/")
        assert response.json() == {
            "detail": "Not allowed: http://169.254.169.254: its address isn't allowed"
        }
        response = register(client, "https://intranet.example.com/")
        assert "10.0.0.7" not in response.text  # never the address it resolved to

    def test_only_the_webs_ports(self, client: TestClient, receiver: Receiver) -> None:
        response = register(client, "https://hooks.example.com:6379/")
        assert response.json() == {
            "detail": "Not allowed: https://hooks.example.com:6379: port 6379 isn't allowed"
        }
        assert receiver.requests == []

    def test_a_url_that_refuses_the_ping(self, client: TestClient, receiver: Receiver) -> None:
        receiver.status = 404
        response = register(client)
        assert (response.status_code, response.json()) == (422, {"detail": "The ping got HTTP 404"})
        assert shop(client).endpoints == {}

    def test_a_url_that_cant_be_reached(self, client: TestClient, receiver: Receiver) -> None:
        receiver.error = httpx.ConnectError("connection refused")
        response = register(client)
        assert response.json() == {"detail": "The ping failed: ConnectError"}
        assert register(client, "https://nowhere.example.com/").json() == {
            "detail": "The ping failed: ConnectError"  # no DNS record
        }

    def test_only_web_urls(self, client: TestClient) -> None:
        assert register(client, "file:///etc/passwd").status_code == 422
        assert register(client, "gopher://hooks.example.com/").status_code == 422


class TestSending:
    def test_every_endpoint_gets_the_event_signed_with_its_secret(
        self,
        client: TestClient,
        receiver: Receiver,
    ) -> None:
        secrets = {
            host: register(client, f"https://{host}/hooks").json()["secret"]
            for host in ("hooks.example.com", "other.example.org")
        }
        assert pay(client, event("ord_9", 4200)).status_code == 204
        sent = [r for r in receiver.requests if json.loads(r.content)["type"] == "order.paid"]
        assert {r.headers["host"] for r in sent} == set(secrets)
        for request in sent:
            message = FastAPIWebhooks(secrets[request.headers["host"]]).verify(
                request.headers, request.content
            )
            assert message.json() == {
                "id": message.id,
                "type": "order.paid",
                "order_id": "ord_9",
                "amount": 4200,
            }
        assert sent[0].headers["webhook-id"] == sent[1].headers["webhook-id"]

    def test_the_delivery_log(self, client: TestClient) -> None:
        endpoint = register(client).json()["id"]
        pay(client)
        [delivery] = client.get(f"/endpoints/{endpoint}/deliveries").json()
        assert (delivery["type"], delivery["outcome"]) == ("order.paid", "delivered")
        assert client.get("/endpoints/ep_unknown/deliveries").status_code == 404

    def test_an_endpoint_that_keeps_failing_is_left_alone_for_a_while(
        self,
        client: TestClient,
        receiver: Receiver,
    ) -> None:
        endpoint = register(client).json()["id"]
        receiver.status = 503
        for n in range(6):
            pay(client, event(f"ord_{n}"))
        outcomes = [d["outcome"] for d in client.get(f"/endpoints/{endpoint}/deliveries").json()]
        assert outcomes == ["HTTP 503"] * 5 + ["skipped"]
        assert receiver.events() == ["ping"] + ["order.paid"] * 5  # the sixth wasn't sent

    def test_a_name_that_now_points_to_a_private_address(
        self,
        client: TestClient,
        dns: DNS,
        receiver: Receiver,
    ) -> None:
        endpoint = register(client).json()["id"]
        dns.records["hooks.example.com"] = ["10.0.0.7"]
        pay(client)
        [delivery] = client.get(f"/endpoints/{endpoint}/deliveries").json()
        assert delivery["outcome"] == "blocked"
        assert receiver.events() == ["ping"]

    def test_an_endpoint_that_cant_be_reached(self, client: TestClient, receiver: Receiver) -> None:
        endpoint = register(client).json()["id"]
        receiver.error = httpx.ConnectError("connection refused")
        pay(client)
        [delivery] = client.get(f"/endpoints/{endpoint}/deliveries").json()
        assert delivery["outcome"] == "ConnectError"
