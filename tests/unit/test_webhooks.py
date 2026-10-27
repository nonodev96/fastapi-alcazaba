"""Standard Webhooks: signing, verifying, replays and the body left for the endpoint."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import base64
import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretBytes, SecretStr

from fastapi_alcazaba import Cache, FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.webhooks import FastAPIWebhooks, WebhookMessage, WebhookSigner

NOW = 1_800_000_000
# The test vector of the Standard Webhooks reference libraries.
SPEC_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"  # noqa: S105  # public
SECRET = "whsec_" + base64.b64encode(b"s" * 32).decode()
OLD_SECRET = "whsec_" + base64.b64encode(b"o" * 32).decode()
BODY = b'{"event": "paid", "amount": 42}'


class Clock:
    """Stands in for the `time` module of fastapi_alcazaba.webhooks."""

    def __init__(self) -> None:
        self.now = float(NOW)

    def time(self) -> float:
        return self.now


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr("fastapi_alcazaba.webhooks.signing.time", clock)
    return clock


def signed(body: bytes = BODY, **options: Any) -> dict[str, str]:
    options.setdefault("message_id", "msg_1")
    options.setdefault("timestamp", NOW)
    return WebhookSigner(options.pop("secret", SECRET)).sign(body, **options)


class Down(InMemoryBackend):
    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        raise ConnectionError("down")


class NoCounters(InMemoryBackend):
    supports_counters = False


class Payment(BaseModel):
    event: str
    amount: int


def app_with(webhooks: FastAPIWebhooks) -> TestClient:
    app = FastAPI()

    @app.post("/model")
    async def with_a_model(
        payment: Payment,
        message: Annotated[WebhookMessage, Depends(webhooks)],
    ) -> dict[str, Any]:
        return {"amount": payment.amount, "id": message.id, "parsed": message.json()}

    @app.post("/raw")
    async def raw(
        request: Request,
        message: Annotated[WebhookMessage, Depends(webhooks)],
    ) -> dict[str, Any]:
        return {"json": await request.json(), "same": await request.body() == message.body}

    return TestClient(app)


@pytest.fixture(name="backend")
def fixture_backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture(name="client")
# pylint: disable-next=unused-argument  # requested for its effect: a frozen clock
def fixture_client(clock: Clock, backend: InMemoryBackend) -> TestClient:
    return app_with(FastAPIWebhooks(SECRET, cache=Cache(backend)))


class TestSigner:
    def test_the_reference_test_vector(self) -> None:
        headers = WebhookSigner(SPEC_SECRET).sign(
            b'{"test": 2432232314}', message_id="msg_p5jXN8AQM9LWM0D4loKWxJek", timestamp=1614265330
        )
        assert headers == {
            "webhook-id": "msg_p5jXN8AQM9LWM0D4loKWxJek",
            "webhook-timestamp": "1614265330",
            "webhook-signature": "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=",
        }

    def test_generated_secrets(self) -> None:
        secret = WebhookSigner.generate_secret()
        assert secret.startswith("whsec_")
        assert len(base64.b64decode(secret.removeprefix("whsec_"))) == 32
        assert secret != WebhookSigner.generate_secret()

    @pytest.mark.usefixtures("clock")
    def test_defaults(self) -> None:
        headers = WebhookSigner(SECRET).sign("text body")
        assert headers["webhook-timestamp"] == str(NOW)
        assert headers["webhook-id"].startswith("msg_")
        assert len(headers["webhook-id"]) == 36
        assert headers == WebhookSigner(SECRET).sign(b"text body", message_id=headers["webhook-id"])

    def test_aware_datetimes(self) -> None:
        moment = datetime.fromtimestamp(NOW, timezone(timedelta(hours=2)))
        assert signed(timestamp=moment) == signed()

    def test_secrets_from_pydantic_settings(self) -> None:
        assert signed(secret=SecretStr(SECRET)) == signed()
        assert signed(secret=SecretBytes(b"s" * 32)) == signed()
        assert signed(secret=[SecretStr(SECRET), OLD_SECRET]) == signed(secret=[SECRET, OLD_SECRET])
        with pytest.raises(ValueError, match="at least 24 bytes"):
            WebhookSigner(SecretBytes(b"s" * 23))

    def test_every_secret_signs_while_rotating(self) -> None:
        signatures = signed(secret=[SECRET, OLD_SECRET])["webhook-signature"].split(" ")
        assert signatures == [
            signed()["webhook-signature"],
            signed(secret=OLD_SECRET)["webhook-signature"],
        ]

    @pytest.mark.parametrize(
        ("secret", "message"),
        [
            ("whsec_not base64!", "base64"),
            ("whsec_" + base64.b64encode(b"s" * 23).decode(), "at least 24 bytes"),
            (b"s" * 23, "at least 24 bytes"),
            ([], "at least one"),
        ],
    )
    def test_invalid_secrets(self, secret: Any, message: str) -> None:
        with pytest.raises(ValueError, match=message) as error:
            WebhookSigner(secret)
        assert "not base64" not in str(error.value)  # secrets don't end up in messages

    def test_secrets_without_prefix_or_as_bytes(self) -> None:
        bare = SECRET.removeprefix("whsec_")
        assert signed(secret=bare) == signed(secret=b"s" * 32) == signed()

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"message_id": ""}, "message_id"),
            ({"message_id": "has space"}, "message_id"),
            ({"message_id": "x" * 257}, "message_id"),
            ({"message_id": "é"}, "message_id"),
            ({"timestamp": datetime(2030, 1, 1)}, "naive"),
            ({"timestamp": -1}, "1970"),
        ],
    )
    def test_invalid_options(self, options: dict[str, Any], message: str) -> None:
        options.setdefault("message_id", "msg_1")
        options.setdefault("timestamp", NOW)
        with pytest.raises(ValueError, match=message):
            WebhookSigner(SECRET).sign(BODY, **options)


class TestVerify:
    @pytest.mark.usefixtures("clock")
    def test_a_valid_webhook(self) -> None:
        message = FastAPIWebhooks(SECRET).verify(signed(), BODY)
        assert message == WebhookMessage("msg_1", datetime.fromtimestamp(NOW, UTC), BODY)
        assert message.json() == {"event": "paid", "amount": 42}

    @pytest.mark.parametrize("header", ["webhook-id", "webhook-timestamp", "webhook-signature"])
    @pytest.mark.usefixtures("clock")
    def test_missing_headers(self, header: str) -> None:
        headers = signed()
        del headers[header]
        with pytest.raises(HTTPException) as error:
            FastAPIWebhooks(SECRET).verify(headers, BODY)
        assert (error.value.status_code, error.value.detail) == (400, "Invalid webhook")

    @pytest.mark.parametrize(
        ("header", "value"),
        [
            ("webhook-id", "with space"),
            ("webhook-id", "x" * 257),
            ("webhook-id", "é"),
            ("webhook-timestamp", "soon"),
            ("webhook-timestamp", "١٢"),  # Arabic-Indic digits: isdigit() but not ASCII
            ("webhook-timestamp", "1" * 13),
            ("webhook-timestamp", "-5"),
            ("webhook-timestamp", ""),
        ],
    )
    @pytest.mark.usefixtures("clock")
    def test_malformed_headers(self, header: str, value: str) -> None:
        headers = {**signed(), header: value}
        with pytest.raises(HTTPException) as error:
            FastAPIWebhooks(SECRET).verify(headers, BODY)
        assert error.value.status_code == 400

    @pytest.mark.parametrize("offset", [-301, 301])
    def test_timestamps_beyond_the_tolerance(self, clock: Clock, offset: int) -> None:
        clock.now += offset
        with pytest.raises(HTTPException) as error:
            FastAPIWebhooks(SECRET).verify(signed(), BODY)
        assert (error.value.status_code, error.value.detail) == (401, "Invalid webhook")

    @pytest.mark.parametrize("offset", [-300, 300])
    def test_timestamps_within_it_either_way(self, clock: Clock, offset: int) -> None:
        clock.now += offset
        assert FastAPIWebhooks(SECRET).verify(signed(), BODY).id == "msg_1"

    def test_another_tolerance(self, clock: Clock) -> None:
        clock.now += 30
        with pytest.raises(HTTPException):
            FastAPIWebhooks(SECRET, tolerance=10).verify(signed(), BODY)

    @pytest.mark.parametrize(
        "change",
        [
            {"body": BODY + b" "},
            {"webhook-id": "msg_2"},
            {"webhook-timestamp": str(NOW + 1)},
            {"webhook-timestamp": f"0{NOW}"},  # the same number, but not what was signed
            {"secret": OLD_SECRET},
        ],
    )
    @pytest.mark.usefixtures("clock")
    def test_anything_changed_gets_the_same_401(self, change: dict[str, Any]) -> None:
        body = change.pop("body", BODY)
        headers = {**signed(secret=change.pop("secret", SECRET)), **change}
        with pytest.raises(HTTPException) as error:
            FastAPIWebhooks(SECRET).verify(headers, body)
        assert (error.value.status_code, error.value.detail) == (401, "Invalid webhook")

    @pytest.mark.parametrize(
        "signatures",
        [
            "v1a,{good}",  # Ed25519, not supported
            "v1,not-base64!",
            "v1,",
            "{good}",
            "v2,{good}",
            "v1," + base64.b64encode(b"x" * 32).decode(),
        ],
    )
    @pytest.mark.usefixtures("clock")
    def test_signatures_that_dont_count(self, signatures: str) -> None:
        good = signed()["webhook-signature"].removeprefix("v1,")
        headers = {**signed(), "webhook-signature": signatures.format(good=good)}
        with pytest.raises(HTTPException) as error:
            FastAPIWebhooks(SECRET).verify(headers, BODY)
        assert error.value.status_code == 401

    @pytest.mark.usefixtures("clock")
    def test_one_valid_signature_among_others(self) -> None:
        good = signed()["webhook-signature"]
        noise = "v1a,abc v1,!!! " + signed(secret=OLD_SECRET)["webhook-signature"]
        headers = {**signed(), "webhook-signature": f"{noise}  {good}"}
        assert FastAPIWebhooks(SECRET).verify(headers, BODY).id == "msg_1"

    @pytest.mark.usefixtures("clock")
    def test_a_signature_header_too_long(self) -> None:
        good = signed()["webhook-signature"]
        headers = {**signed(), "webhook-signature": "v1,x " * 1_000 + good}
        with pytest.raises(HTTPException) as error:
            FastAPIWebhooks(SECRET).verify(headers, BODY)
        assert error.value.status_code == 401

    @pytest.mark.usefixtures("clock")
    def test_rotation_accepts_both_secrets(self) -> None:
        webhooks = FastAPIWebhooks([SECRET, OLD_SECRET])
        assert webhooks.verify(signed(secret=OLD_SECRET), BODY).id == "msg_1"
        assert webhooks.verify(signed(), BODY).id == "msg_1"

    @pytest.mark.parametrize(
        ("options", "message"),
        [({"tolerance": 0}, "tolerance"), ({"max_body_size": -1}, "max_body_size")],
    )
    def test_validation(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            FastAPIWebhooks(SECRET, **options)


class TestDependency:
    def test_the_endpoint_still_reads_the_body(self, client: TestClient) -> None:
        json_type = {"content-type": "application/json"}
        response = client.post("/model", content=BODY, headers={**signed(), **json_type})
        assert response.status_code == 200, response.text
        assert response.json() == {
            "amount": 42,
            "id": "msg_1",
            "parsed": {"event": "paid", "amount": 42},
        }
        raw = client.post("/raw", content=BODY, headers=signed(message_id="msg_2"))
        assert raw.json() == {"json": {"event": "paid", "amount": 42}, "same": True}

    def test_a_body_sent_in_chunks(self, client: TestClient) -> None:
        chunks = iter([BODY[:10], BODY[10:]])
        response = client.post("/raw", content=chunks, headers=signed())
        assert response.json()["same"] is True

    def test_a_replay_gets_the_same_401_as_a_bad_signature(self, client: TestClient) -> None:
        headers = signed()
        assert client.post("/raw", content=BODY, headers=headers).status_code == 200
        replay = client.post("/raw", content=BODY, headers=headers)
        forged = client.post("/raw", content=BODY, headers={**headers, "webhook-id": "msg_9"})
        assert replay.status_code == forged.status_code == 401
        assert replay.json() == forged.json() == {"detail": "Invalid webhook"}

    def test_a_retry_of_the_sender_gets_through(self, client: TestClient, clock: Clock) -> None:
        """A retry keeps the message id, with a new timestamp and signature."""
        assert client.post("/raw", content=BODY, headers=signed()).status_code == 200
        clock.now += 30
        retry = WebhookSigner(SECRET).sign(BODY, message_id="msg_1")
        assert client.post("/raw", content=BODY, headers=retry).status_code == 200

    def test_replay_marks_are_hashed_and_outlive_the_window(
        self,
        client: TestClient,
        backend: InMemoryBackend,
    ) -> None:
        assert client.post("/raw", content=BODY, headers=signed()).status_code == 200
        [key] = [key for key in backend._state if key.startswith("!webhook:")]
        assert "msg_1" not in key
        ttl, count = asyncio.run(backend.get_with_ttl(key))
        assert (count, 600 < ttl <= 660) == (b"1", True)  # twice the tolerance, and a margin

    def test_failed_checks_dont_burn_the_id(self, client: TestClient) -> None:
        """Only accepted deliveries are counted: a forged copy doesn't block the real one."""
        forged = {**signed(), "webhook-signature": signed(secret=OLD_SECRET)["webhook-signature"]}
        assert client.post("/raw", content=BODY, headers=forged).status_code == 401
        assert client.post("/raw", content=BODY, headers=signed()).status_code == 200

    @pytest.mark.usefixtures("clock")
    def test_bodies_too_large(self, backend: InMemoryBackend) -> None:
        client = app_with(FastAPIWebhooks(SECRET, cache=Cache(backend), max_body_size=10))
        declared = client.post("/raw", content=BODY, headers=signed())
        chunked = client.post("/raw", content=iter([BODY[:8], BODY[8:]]), headers=signed())
        assert declared.status_code == chunked.status_code == 413
        assert client.post("/raw", content=b"{}", headers=signed(b"{}")).status_code == 200

    async def test_an_absurd_content_length(self) -> None:
        async def receive() -> dict[str, Any]:
            raise AssertionError("not read")  # pragma: no cover

        scope = {"type": "http", "headers": [(b"content-length", b"9" * 30)]}
        with pytest.raises(HTTPException) as error:
            await FastAPIWebhooks(SECRET)._read_body(Request(scope, receive))
        assert error.value.status_code == 413

    @pytest.mark.usefixtures("clock")
    def test_the_backend_down_refuses_with_a_503(self, caplog: pytest.LogCaptureFixture) -> None:
        client = app_with(FastAPIWebhooks(SECRET, cache=Cache(Down())))
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.webhooks.signing"):
            response = client.post("/raw", content=BODY, headers=signed())
        assert (response.status_code, response.headers["retry-after"]) == (503, "5")
        assert "refused" in caplog.text

    @pytest.mark.usefixtures("clock")
    def test_fail_open_accepts_without_the_replay_check(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = app_with(FastAPIWebhooks(SECRET, cache=Cache(Down()), fail_open=True))
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.webhooks.signing"):
            assert client.post("/raw", content=BODY, headers=signed()).status_code == 200
            assert client.post("/raw", content=BODY, headers=signed()).status_code == 200
        assert "accepted" in caplog.text

    @pytest.mark.usefixtures("clock")
    def test_a_backend_without_counters(self) -> None:
        client = app_with(FastAPIWebhooks(SECRET, cache=Cache(NoCounters())))
        with pytest.raises(NotImplementedError, match="counters"):
            client.post("/raw", content=BODY, headers=signed())

    @pytest.mark.usefixtures("clock")
    def test_the_installed_cache_by_default(self) -> None:
        backend = InMemoryBackend()
        client = app_with(FastAPIWebhooks(SECRET))
        Cache(backend).install(client.app)
        assert client.post("/raw", content=BODY, headers=signed()).status_code == 200
        assert any(key.startswith("!webhook:") for key in backend._state)

    @pytest.mark.usefixtures("clock")
    def test_fastapi_cache_otherwise(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, prefix="app")
        client = app_with(FastAPIWebhooks(SECRET, namespace="hooks"))
        assert client.post("/raw", content=BODY, headers=signed()).status_code == 200
        assert any(key.startswith("app!hooks:") for key in backend._state)
