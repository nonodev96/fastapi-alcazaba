"""Signed webhooks, compatible with Standard Webhooks (https://www.standardwebhooks.com): the
format of Svix, Supabase, Clerk, Resend, OpenAI and others.

Receiving them:

    webhooks = FastAPIWebhooks(os.environ["WEBHOOK_SECRET"])  # "whsec_..."

    @app.post("/webhooks/payments")
    async def payment_event(message: Annotated[WebhookMessage, Depends(webhooks)]) -> None:
        event = message.json()
        ...  # idempotent on message.id: the sender retries failed deliveries with the same id

Sending them:

    signer = WebhookSigner(secret)
    body = json.dumps(event).encode()
    await client.post(url, content=body, headers=signer.sign(body))

Each message carries `webhook-id`, `webhook-timestamp` (Unix seconds) and `webhook-signature`:
`v1,<base64 HMAC-SHA256 of "id.timestamp.body">`, several of them separated by spaces while a
secret is being rotated. The receiver checks a signature over the raw body, refuses timestamps
too far from its clock, and remembers every delivery it accepted until its timestamp is too old,
so that a captured request can't be sent again.
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request

from fastapi_alcazaba.cache import Cache, get_cache, metrics_of
from fastapi_alcazaba.errors import BackendUnavailable
from fastapi_alcazaba.tokens import _shared

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: Secrets of Standard Webhooks: `whsec_<base64>` strings (or the base64 alone) or raw bytes. A
#: list rotates them.
WebhookSecrets = str | bytes | Sequence[str | bytes]

PREFIX = "whsec_"
#: The shortest secret the specification allows (192 bits).
MIN_SECRET_SIZE = 24
_MAX_ID = 256  # characters of a webhook-id
_MAX_TIMESTAMP = 12  # digits: Unix seconds until the year 33658
_MAX_SIGNATURES = 4_096  # characters of the webhook-signature header
_INVALID = "Invalid webhook"
# Clocks of different processes disagree a little: replay marks outlive the window by this much.
_CLOCK_MARGIN = 60


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, bytes):
        raw = secret
    else:
        try:
            raw = base64.b64decode(secret.removeprefix(PREFIX), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(
                "a webhook secret is 'whsec_' followed by base64 (or raw bytes); "
                "generate one with WebhookSigner.generate_secret()"
            ) from None
    if len(raw) < MIN_SECRET_SIZE:
        raise ValueError(f"webhook secrets must be at least {MIN_SECRET_SIZE} bytes long")
    return raw


def _secret_list(secret: WebhookSecrets) -> list[bytes]:
    if isinstance(secret, str | bytes):
        secret = [secret]
    if not secret:
        raise ValueError("at least one secret is needed")
    return [_secret_bytes(item) for item in secret]


def _valid_id(message_id: str) -> bool:
    return 1 <= len(message_id) <= _MAX_ID and all("!" <= char <= "~" for char in message_id)


def _mac(secret: bytes, message_id: str, timestamp: str, body: bytes) -> bytes:
    mac = hmac.new(secret, f"{message_id}.{timestamp}.".encode(), hashlib.sha256)
    mac.update(body)
    return mac.digest()


# What the counted outcome of a refused delivery is, by status.
_REFUSALS = {400: "malformed", 401: "rejected", 413: "too_large"}


@dataclass(frozen=True, slots=True)
class WebhookMessage:
    """A webhook whose signature was checked: what `FastAPIWebhooks` gives the endpoint."""

    #: `webhook-id`: the same for every delivery attempt of a message.
    id: str
    #: `webhook-timestamp`, in UTC: when this attempt was signed.
    timestamp: datetime
    #: The raw body, exactly as signed.
    body: bytes

    def json(self) -> Any:
        """The body, parsed as JSON."""
        return json.loads(self.body)


class WebhookSigner:
    """Signs outgoing webhooks the Standard Webhooks way.

        signer = WebhookSigner(os.environ["WEBHOOK_SECRET"])
        body = json.dumps(event).encode()
        await client.post(url, content=body, headers=signer.sign(body))

    Sign the exact bytes you send: `json=event` would serialize them again, maybe differently.

    :param secret: `whsec_<base64>` (`generate_secret()`), the base64 alone, or raw bytes; at
        least 24 bytes once decoded. With a list, every message is signed with each of them, so
        that receivers that have the old secret and those that have the new one both accept it
        while it's being rotated.
    """

    def __init__(self, secret: WebhookSecrets) -> None:
        self._secrets = _secret_list(secret)

    @staticmethod
    def generate_secret() -> str:
        """A new random secret (256 bits), as `whsec_<base64>`."""
        return PREFIX + base64.b64encode(secrets.token_bytes(32)).decode()

    def sign(
        self,
        body: bytes | str,
        *,
        message_id: str | None = None,
        timestamp: float | datetime | None = None,
    ) -> dict[str, str]:
        """The headers of a webhook carrying `body`: `webhook-id`, `webhook-timestamp` and
        `webhook-signature`. Give the same `message_id` to every attempt of a message (a random
        one by default) and leave `timestamp` (now) alone: each attempt is signed afresh."""
        if isinstance(body, str):
            body = body.encode()
        if message_id is None:
            message_id = f"msg_{secrets.token_hex(16)}"
        elif not _valid_id(message_id):
            raise ValueError(f"message_id must be 1 to {_MAX_ID} printable ASCII characters")
        if timestamp is None:
            seconds = int(time.time())
        elif isinstance(timestamp, datetime):
            if timestamp.utcoffset() is None:
                raise ValueError("naive datetimes are ambiguous: give it a time zone (e.g. UTC)")
            seconds = int(timestamp.timestamp())
        else:
            seconds = int(timestamp)
        if seconds < 0:
            raise ValueError("the timestamp can't be before 1970")
        sent = str(seconds)
        signatures = " ".join(
            "v1," + base64.b64encode(_mac(secret, message_id, sent, body)).decode()
            for secret in self._secrets
        )
        return {
            "webhook-id": message_id,
            "webhook-timestamp": sent,
            "webhook-signature": signatures,
        }


class FastAPIWebhooks:
    """FastAPI dependency that accepts only webhooks signed with `secret`, and each delivery once.

        webhooks = FastAPIWebhooks(os.environ["WEBHOOK_SECRET"])

        @app.post("/webhooks")
        async def receive(message: Annotated[WebhookMessage, Depends(webhooks)]) -> None: ...

    It reads the raw body (up to `max_body_size` bytes, a 413 beyond) and leaves it in the request:
    the endpoint can still take a pydantic model, `await request.json()` or `message.json()`. It
    can't read a body the endpoint takes as a form, which consumes it first: webhooks are JSON.

    Missing or malformed headers get a 400; a wrong signature, a timestamp more than `tolerance`
    seconds away from the server's clock (either way) and a delivery accepted before all get the
    same 401, which doesn't say which check failed.

    Replays: each delivery accepted (its id and timestamp, hashed) is counted in the cache's
    backend until its timestamp can't be accepted anymore, so every process refuses it again. A
    retry of the sender has a new timestamp and signature and goes through: Standard Webhooks
    keeps the id of a message for all its attempts, and a retry after a failed (5xx) attempt must
    get through. So make the endpoint idempotent on `message.id` (a unique constraint), as the
    specification advises, if a message must not be processed twice. Whoever can write to the
    backend can delete the marks (and allow a replay within `tolerance`): keep it out of reach.

    :param secret: `whsec_<base64>`, the base64 alone or raw bytes, at least 24 bytes once
        decoded. A list rotates them: a signature by any of them is accepted.
    :param tolerance: seconds a timestamp may be away from the server's clock, either way.
    :param cache: the Cache whose backend counts the deliveries (it must support counters); the
        app's (`Cache.install`) or FastAPICache's by default.
    :param max_body_size: larger bodies get a 413 without being read to the end.
    :param namespace: part of the keys, after the cache's prefix (see `Cache.state_key`).
    :param fail_open: when the backend fails, accept the (correctly signed) webhook without the
        replay check instead of answering 503. Off by default: senders retry a 503 later, while a
        replay would run its effects again.
    """

    def __init__(
        self,
        secret: WebhookSecrets,
        *,
        tolerance: int = 300,
        cache: Cache | None = None,
        max_body_size: int = 1_048_576,
        namespace: str = "webhook",
        fail_open: bool = False,
    ) -> None:
        if tolerance < 1:
            raise ValueError("tolerance must be at least 1 second")
        if max_body_size < 0:
            raise ValueError("max_body_size can't be negative")
        self._secrets = _secret_list(secret)
        self.tolerance = tolerance
        self.cache = cache
        self.max_body_size = max_body_size
        self.namespace = namespace
        self.fail_open = fail_open

    async def __call__(self, request: Request) -> WebhookMessage:
        try:
            body = await self._read_body(request)
            message = self.verify(request.headers, body)
        except HTTPException as exc:
            self._count(request, _REFUSALS.get(exc.status_code, "rejected"))
            raise
        self._count(request, await self._first_delivery(request, message))
        return message

    def _count(self, request: Request, outcome: str) -> None:
        metrics = self.cache.metrics if self.cache is not None else metrics_of(request)
        metrics.count("webhooks", outcome=outcome)

    def verify(self, headers: Mapping[str, str], body: bytes) -> WebhookMessage:
        """The message, if `headers` carry a valid signature of `body` and a fresh timestamp;
        raises an HTTPException (400 or 401) otherwise. It doesn't check for replays: for tests,
        scripts and queues that deduplicate on their own."""
        message_id = headers.get("webhook-id")
        sent = headers.get("webhook-timestamp")
        signatures = headers.get("webhook-signature")
        if message_id is None or sent is None or signatures is None:
            raise HTTPException(400, _INVALID)
        if not (
            _valid_id(message_id)
            and 1 <= len(sent) <= _MAX_TIMESTAMP
            and sent.isascii()
            and sent.isdigit()
        ):
            raise HTTPException(400, _INVALID)
        timestamp = int(sent)
        if abs(time.time() - timestamp) > self.tolerance or len(signatures) > _MAX_SIGNATURES:
            raise HTTPException(401, _INVALID)
        # The timestamp as sent, not as parsed: it's what was signed.
        expected = [_mac(secret, message_id, sent, body) for secret in self._secrets]
        for signature in signatures.split(" "):
            version, _, encoded = signature.partition(",")
            if version != "v1":
                continue  # another scheme (v1a is Ed25519), or a stray space
            try:
                candidate = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                continue
            if any(hmac.compare_digest(candidate, mac) for mac in expected):
                return WebhookMessage(message_id, datetime.fromtimestamp(timestamp, UTC), body)
        raise HTTPException(401, _INVALID)

    async def _read_body(self, request: Request) -> bytes:
        declared = request.headers.get("content-length", "")
        if (
            declared.isascii()
            and declared.isdigit()
            and (len(declared) > 18 or int(declared) > self.max_body_size)
        ):
            raise HTTPException(413, "Request body too large")
        chunks: list[bytes] = []
        size = 0
        # Gives the body back if FastAPI read it already (for a body parameter of the endpoint).
        async with aclosing(request.stream()) as stream:
            async for chunk in stream:
                size += len(chunk)
                if size > self.max_body_size:
                    raise HTTPException(413, "Request body too large")
                chunks.append(chunk)
        body = b"".join(chunks)
        # Where Request.body() keeps it: the endpoint can read the body again.
        vars(request)["_body"] = body
        return body

    async def _first_delivery(self, request: Request, message: WebhookMessage) -> str:
        """Count the delivery: "accepted" ("unchecked" when the backend failed and `fail_open`);
        a 401 if it was accepted before, a 503 if the backend fails."""
        cache = self.cache if self.cache is not None else get_cache(request)
        backend = _shared(cache.backend)
        if not backend.supports_counters:
            raise NotImplementedError(
                f"{type(backend).__name__} can't keep counters, which FastAPIWebhooks needs"
            )
        sent = int(message.timestamp.timestamp())
        digest = hashlib.sha256(json.dumps([message.id, sent]).encode()).hexdigest()
        key = cache.state_key(self.namespace, digest)
        # Accepted until timestamp + tolerance, and it may be up to `tolerance` in the future.
        expire = 2 * self.tolerance + _CLOCK_MARGIN
        try:
            deliveries = await cache.guard.run("incr", lambda: backend.incr(key, 1, expire))
        except BackendUnavailable as exc:
            verdict = "accepted" if self.fail_open else "refused"
            logger.warning("Couldn't check a webhook for replays: %s", verdict, exc_info=exc)
            if self.fail_open:
                return "unchecked"
            self._count(request, "unavailable")
            raise HTTPException(503, "Service Unavailable", headers={"Retry-After": "5"}) from exc
        if deliveries > 1:
            logger.warning("Refused a replayed webhook (id %r)", message.id)
            self._count(request, "replayed")
            raise HTTPException(401, _INVALID)
        return "accepted"
