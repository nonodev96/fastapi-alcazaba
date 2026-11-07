"""The senders FastAPIWebhooks verifies besides Standard Webhooks' HMAC: its Ed25519 signatures
(v1a), GitHub, Stripe and Slack, with their published examples, through the same checks."""

import base64
import hashlib
import hmac
import time
from typing import Annotated, Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from fastapi_alcazaba import Cache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.webhooks import FastAPIWebhooks, WebhookMessage, WebhookSigner
from tests.unit.test_webhooks import BODY, NOW, SECRET, Clock, fixture_clock

__all__ = ["fixture_clock"]  # the `clock` fixture, from the Standard Webhooks tests

GITHUB_SECRET = "It's a Secret to Everybody"  # noqa: S105  # GitHub's documentation's
SLACK_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"  # noqa: S105  # Slack's documentation's
STRIPE_SECRET = "whsec_test_secret_of_the_tests"  # noqa: S105  # the tests' own
SLACK_BODY = (
    b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V"
    b"&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text="
    b"&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmib"
    b"IGlgcZRskXaIFfN&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
)
SLACK_TIME = 1531420618


def refused(webhooks: FastAPIWebhooks, headers: dict[str, str], body: bytes = BODY) -> int:
    with pytest.raises(HTTPException) as caught:
        webhooks.verify(headers, body)
    assert caught.value.detail == "Invalid webhook"
    return caught.value.status_code


def stripe_headers(
    body: bytes = BODY,
    at: int = NOW,
    secret: str = STRIPE_SECRET,
) -> dict[str, str]:
    mac = hmac.new(secret.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={at},v1={mac},v0=ignored"}


class TestEd25519:
    def test_signed_with_the_private_key_verified_with_the_public_one(self, clock: Clock) -> None:
        private, public = WebhookSigner.generate_key_pair()
        assert private.startswith("whsk_")
        assert public.startswith("whpk_")
        headers = WebhookSigner(private).sign(BODY, message_id="msg_1", timestamp=NOW)
        assert headers["webhook-signature"].startswith("v1a,")
        message = FastAPIWebhooks(public).verify(headers, BODY)
        assert (message.id, message.body) == ("msg_1", BODY)
        assert refused(FastAPIWebhooks(public), headers, BODY + b" ") == 401
        other = WebhookSigner.generate_key_pair()[1]
        assert refused(FastAPIWebhooks(other), headers) == 401
        del clock

    def test_what_any_ed25519_library_signs(self, clock: Clock) -> None:
        """The signature is Ed25519 of "id.timestamp.body", as the specification says."""
        key = ed25519.Ed25519PrivateKey.generate()
        signature = key.sign(f"msg_2.{NOW}.".encode() + BODY)
        public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
        headers = {
            "webhook-id": "msg_2",
            "webhook-timestamp": str(NOW),
            "webhook-signature": "v1a," + base64.b64encode(signature).decode(),
        }
        assert FastAPIWebhooks(f"whpk_{public}").verify(headers, BODY).id == "msg_2"
        del clock

    def test_hmac_and_ed25519_together(self, clock: Clock) -> None:
        private, public = WebhookSigner.generate_key_pair()
        headers = WebhookSigner([SECRET, private]).sign(BODY, message_id="m", timestamp=NOW)
        assert [part[:3] for part in headers["webhook-signature"].split(" ")] == ["v1,", "v1a"]
        assert FastAPIWebhooks(SECRET).verify(headers, BODY).id == "m"
        assert FastAPIWebhooks(public).verify(headers, BODY).id == "m"
        del clock

    def test_a_libsodium_secret_key(self, clock: Clock) -> None:
        """libsodium keeps the seed and the public key together: 64 bytes."""
        key = ed25519.Ed25519PrivateKey.generate()
        seed = key.private_bytes_raw()
        both = base64.b64encode(seed + key.public_key().public_bytes_raw()).decode()
        public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
        headers = WebhookSigner(f"whsk_{both}").sign(BODY, timestamp=NOW)
        assert FastAPIWebhooks(f"whpk_{public}").verify(headers, BODY)
        del clock

    @pytest.mark.parametrize(
        ("secret", "message"),
        [
            ("whpk_" + base64.b64encode(b"x" * 31).decode(), "32 bytes"),
            ("whsk_" + base64.b64encode(b"x" * 40).decode(), "32 or 64 bytes"),
            ("whpk_not base64", "base64"),
        ],
    )
    def test_invalid_keys(self, secret: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            FastAPIWebhooks(secret)

    def test_a_public_key_doesnt_sign(self) -> None:
        with pytest.raises(ValueError, match="only verifies"):
            WebhookSigner(WebhookSigner.generate_key_pair()[1])


class TestGitHub:
    def test_its_documentation_s_example(self) -> None:
        webhooks = FastAPIWebhooks(GITHUB_SECRET, scheme="github")
        headers = {
            "X-GitHub-Delivery": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
            "X-Hub-Signature-256": (
                "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
            ),
        }
        message = webhooks.verify(headers, b"Hello, World!")
        # X-GitHub-Delivery isn't signed: the body names the message.
        assert message.id == "sha256:" + hashlib.sha256(b"Hello, World!").hexdigest()
        assert message.timestamp is None  # GitHub signs no time
        assert refused(webhooks, headers, b"Hello, World?") == 401

    @pytest.mark.parametrize(
        ("headers", "status"),
        [
            ({"X-GitHub-Delivery": "d1"}, 400),  # no signature
            ({"X-GitHub-Delivery": "d1", "X-Hub-Signature-256": "sha1=" + "0" * 40}, 401),
            ({"X-GitHub-Delivery": "d1", "X-Hub-Signature-256": "sha256=zz"}, 401),
            ({"X-GitHub-Delivery": "d1", "X-Hub-Signature-256": "sha256=" + "é" * 64}, 401),
            ({"X-GitHub-Delivery": "d1", "X-Hub-Signature-256": "sha256=" + "g" * 64}, 401),
            ({"X-GitHub-Delivery": "d1", "X-Hub-Signature-256": "s" * 5000}, 401),
        ],
    )
    def test_refused(self, headers: dict[str, str], status: int) -> None:
        assert refused(FastAPIWebhooks(GITHUB_SECRET, scheme="github"), headers) == status

    def test_rotation(self) -> None:
        mac = hmac.new(GITHUB_SECRET.encode(), BODY, hashlib.sha256).hexdigest()
        headers = {"X-GitHub-Delivery": "d1", "X-Hub-Signature-256": f"sha256={mac}"}
        webhooks = FastAPIWebhooks(["a-newer-github-secret", GITHUB_SECRET], scheme="github")
        assert webhooks.verify(headers, BODY).id == "sha256:" + hashlib.sha256(BODY).hexdigest()


class TestStripe:
    def test_signed_and_within_the_tolerance(self, clock: Clock) -> None:
        webhooks = FastAPIWebhooks(STRIPE_SECRET, scheme="stripe")
        message = webhooks.verify(stripe_headers(), BODY)
        assert message.id.startswith("sha256:")
        assert message.timestamp is not None
        assert int(message.timestamp.timestamp()) == NOW
        assert message.id == webhooks.verify(stripe_headers(), BODY).id  # the same delivery
        assert message.id != webhooks.verify(stripe_headers(at=NOW + 1), BODY).id  # a retry
        clock.now += 301
        assert refused(webhooks, stripe_headers()) == 401

    def test_one_of_several_v1_and_the_secret_as_given(self, clock: Clock) -> None:
        good = stripe_headers()["Stripe-Signature"]
        header = f"t={NOW},v1={'0' * 64},{good.split(',')[1]}"
        webhooks = FastAPIWebhooks(STRIPE_SECRET, scheme="stripe")
        assert webhooks.verify({"Stripe-Signature": header}, BODY)
        # Stripe signs with the secret's string, "whsec_" included, not with what it decodes to.
        decoded = base64.b64decode(STRIPE_SECRET.removeprefix("whsec_") + "==")
        assert refused(webhooks, stripe_headers(secret=decoded.decode("latin-1"))) == 401
        del clock

    @pytest.mark.parametrize(
        "header",
        [
            "",
            f"v1={'0' * 64}",  # no time
            f"t={NOW}",  # no signature
            f"t={NOW},v0={'0' * 64}",  # only test-mode's
            f"t=soon,v1={'0' * 64}",
        ],
    )
    def test_malformed(self, header: str, clock: Clock) -> None:
        webhooks = FastAPIWebhooks(STRIPE_SECRET, scheme="stripe")
        assert refused(webhooks, {"Stripe-Signature": header}) == 400
        assert refused(webhooks, {}) == 400
        del clock

    def test_a_wrong_signature(self, clock: Clock) -> None:
        webhooks = FastAPIWebhooks(STRIPE_SECRET, scheme="stripe")
        headers = stripe_headers(secret=STRIPE_SECRET + "-another")
        assert refused(webhooks, headers) == 401
        del clock


class TestSlack:
    def test_its_documentation_s_example(self, clock: Clock) -> None:
        clock.now = SLACK_TIME + 10
        webhooks = FastAPIWebhooks(SLACK_SECRET, scheme="slack")
        headers = {
            "X-Slack-Request-Timestamp": str(SLACK_TIME),
            "X-Slack-Signature": (
                "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"
            ),
        }
        message = webhooks.verify(headers, SLACK_BODY)
        assert message.id.startswith("sha256:")
        assert refused(webhooks, headers, SLACK_BODY + b"&x=1") == 401
        clock.now = SLACK_TIME + 400
        assert refused(webhooks, headers, SLACK_BODY) == 401

    @pytest.mark.parametrize(
        "headers",
        [
            {"X-Slack-Signature": "v0=" + "0" * 64},
            {"X-Slack-Request-Timestamp": str(NOW)},
            {"X-Slack-Request-Timestamp": "-1", "X-Slack-Signature": "v0=" + "0" * 64},
        ],
    )
    def test_malformed(self, headers: dict[str, str], clock: Clock) -> None:
        assert refused(FastAPIWebhooks(SLACK_SECRET, scheme="slack"), headers) == 400
        del clock

    def test_another_version(self, clock: Clock) -> None:
        headers = {"X-Slack-Request-Timestamp": str(NOW), "X-Slack-Signature": "v1=" + "0" * 64}
        assert refused(FastAPIWebhooks(SLACK_SECRET, scheme="slack"), headers) == 401
        del clock


class TestTheSameChecks:
    def app(self, webhooks: FastAPIWebhooks) -> TestClient:
        app = FastAPI()

        @app.post("/hook")
        async def hook(message: Annotated[WebhookMessage, Depends(webhooks)]) -> dict[str, Any]:
            return {"id": message.id}

        return TestClient(app)

    def test_replays_of_every_scheme(self, clock: Clock) -> None:
        cache = Cache(InMemoryBackend())
        mac = hmac.new(GITHUB_SECRET.encode(), BODY, hashlib.sha256).hexdigest()
        cases = [
            (FastAPIWebhooks(STRIPE_SECRET, scheme="stripe", cache=cache), stripe_headers()),
            (
                FastAPIWebhooks(GITHUB_SECRET, scheme="github", cache=cache),
                {"X-GitHub-Delivery": "d1", "X-Hub-Signature-256": f"sha256={mac}"},
            ),
        ]
        for webhooks, headers in cases:
            client = self.app(webhooks)
            assert client.post("/hook", content=BODY, headers=headers).status_code == 200
            replay = client.post("/hook", content=BODY, headers=headers)
            assert (replay.status_code, replay.json()) == (401, {"detail": "Invalid webhook"})
        del clock

    def test_a_github_replay_with_another_delivery_id(self, clock: Clock) -> None:
        """X-GitHub-Delivery isn't signed: changing it doesn't make a copy another message."""
        mac = hmac.new(GITHUB_SECRET.encode(), BODY, hashlib.sha256).hexdigest()
        webhooks = FastAPIWebhooks(GITHUB_SECRET, scheme="github", cache=Cache(InMemoryBackend()))
        client = self.app(webhooks)
        ids = []
        for delivery in ("d1", "d2", "d3"):
            headers = {"X-GitHub-Delivery": delivery, "X-Hub-Signature-256": f"sha256={mac}"}
            response = client.post("/hook", content=BODY, headers=headers)
            ids.append((response.status_code, response.json().get("id")))
        digest = "sha256:" + hashlib.sha256(BODY).hexdigest()
        assert ids == [(200, digest), (401, None), (401, None)]
        del clock

    def test_the_body_size_limit(self, clock: Clock) -> None:
        webhooks = FastAPIWebhooks(
            SLACK_SECRET, scheme="slack", cache=Cache(InMemoryBackend()), max_body_size=10
        )
        response = self.app(webhooks).post("/hook", content=b"x" * 11, headers={})
        assert response.status_code == 413
        del clock

    @pytest.mark.parametrize(
        ("options", "error", "message"),
        [
            ({"secret": "short", "scheme": "github"}, ValueError, "16 bytes"),
            ({"secret": GITHUB_SECRET, "scheme": "gitlab"}, ValueError, "scheme is one of"),
            ({"secret": [], "scheme": "slack"}, ValueError, "at least one secret"),
        ],
    )
    def test_configuration(
        self,
        options: dict[str, Any],
        error: type[Exception],
        message: str,
    ) -> None:
        with pytest.raises(error, match=message):
            FastAPIWebhooks(**options)


def test_now_is_the_real_time_without_the_clock() -> None:
    at = int(time.time())
    assert FastAPIWebhooks(STRIPE_SECRET, scheme="stripe").verify(stripe_headers(at=at), BODY)
