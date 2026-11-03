"""Signed webhooks: receiving those of Standard Webhooks (https://www.standardwebhooks.com, the
format of Svix, Supabase, Clerk, Resend, OpenAI and others), GitHub, Stripe and Slack, and signing
those the app sends, the Standard Webhooks way.

Receiving them:

    webhooks = FastAPIWebhooks(settings.webhook_secret)  # "whsec_..."
    github = FastAPIWebhooks(settings.github_secret, scheme="github")

    @app.post("/webhooks/payments")
    async def payment_event(message: Annotated[WebhookMessage, Depends(webhooks)]) -> None:
        event = message.json()
        ...  # idempotent on message.id: the sender retries failed deliveries with the same id

Sending them:

    signer = WebhookSigner(secret)
    body = json.dumps(event).encode()
    await client.post(url, content=body, headers=signer.sign(body))

A Standard Webhooks message carries `webhook-id`, `webhook-timestamp` (Unix seconds) and
`webhook-signature`: `v1,<base64 HMAC-SHA256 of "id.timestamp.body">` or `v1a,<base64 Ed25519
signature of it>`, several of them separated by spaces while a secret is being rotated. Whatever
the scheme, the receiver checks a signature over the raw body, refuses timestamps too far from its
clock, and remembers every delivery it accepted until its timestamp is too old, so that a captured
request can't be sent again.
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
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import SecretBytes, SecretStr
from starlette.requests import Request

from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.core.integrity import Secret, reveal
from fastapi_alcazaba.response_cache.caches import Cache, get_cache, metrics_of

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: Secrets: `whsec_<base64>` strings (or the base64 alone) or raw bytes for Standard Webhooks'
#: HMAC, `whpk_`/`whsk_` for its Ed25519 keys, the strings GitHub, Stripe or Slack give;
#: also as pydantic's SecretStr or SecretBytes. A list rotates them.
WebhookSecrets = Secret | Sequence[Secret]
#: The senders whose webhooks `FastAPIWebhooks` verifies.
Scheme = Literal["standard", "github", "stripe", "slack"]

PREFIX = "whsec_"
PUBLIC_PREFIX = "whpk_"
PRIVATE_PREFIX = "whsk_"
#: The shortest secret Standard Webhooks allows (192 bits).
MIN_SECRET_SIZE = 24
#: The shortest secret of GitHub, Stripe or Slack accepted.
MIN_PROVIDER_SECRET = 16
_MAX_ID = 256  # characters of a webhook-id
_MAX_TIMESTAMP = 12  # digits: Unix seconds until the year 33658
_MAX_SIGNATURES = 4_096  # characters of a signature header
_INVALID = "Invalid webhook"
# Clocks of different processes disagree a little: replay marks outlive the window by this much.
_CLOCK_MARGIN = 60


def _b64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(
            "a webhook secret is 'whsec_' followed by base64 (or raw bytes); "
            "generate one with WebhookSigner.generate_secret()"
        ) from None


def _ed25519() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover  # the tests have cryptography
        raise ImportError(
            'Ed25519 webhook keys need cryptography: pip install "fastapi-alcazaba[crypto]"'
        ) from exc
    return ed25519


@dataclass(frozen=True, slots=True)
class _Key:
    """A Standard Webhooks key: an HMAC secret, or an Ed25519 public or private key."""

    hmac: bytes | None = None
    public: Any = None
    private: Any = None


def _standard_key(secret: Secret) -> _Key:
    value = reveal(secret)
    if isinstance(value, bytes):
        raw = value
    elif value.startswith(PUBLIC_PREFIX):
        key = _b64(value.removeprefix(PUBLIC_PREFIX))
        if len(key) != 32:
            raise ValueError("a whpk_ key is the base64 of an Ed25519 public key (32 bytes)")
        return _Key(public=_ed25519().Ed25519PublicKey.from_public_bytes(key))
    elif value.startswith(PRIVATE_PREFIX):
        key = _b64(value.removeprefix(PRIVATE_PREFIX))
        if len(key) not in (32, 64):
            raise ValueError("a whsk_ key is the base64 of an Ed25519 seed (32 or 64 bytes)")
        private = _ed25519().Ed25519PrivateKey.from_private_bytes(key[:32])
        return _Key(public=private.public_key(), private=private)
    else:
        raw = _b64(value.removeprefix(PREFIX))
    if len(raw) < MIN_SECRET_SIZE:
        raise ValueError(f"webhook secrets must be at least {MIN_SECRET_SIZE} bytes long")
    return _Key(hmac=raw)


def _items(secret: WebhookSecrets) -> list[Secret]:
    if isinstance(secret, str | bytes | SecretStr | SecretBytes):
        return [secret]
    if not secret:
        raise ValueError("at least one secret is needed")
    return list(secret)


def _provider_secret(secret: Secret) -> bytes:
    value = reveal(secret)
    raw = value if isinstance(value, bytes) else value.encode()
    if len(raw) < MIN_PROVIDER_SECRET:
        raise ValueError(f"webhook secrets must be at least {MIN_PROVIDER_SECRET} bytes long")
    return raw


def _valid_id(message_id: str) -> bool:
    return 1 <= len(message_id) <= _MAX_ID and all("!" <= char <= "~" for char in message_id)


def _timestamp(sent: str) -> bool:
    return 1 <= len(sent) <= _MAX_TIMESTAMP and sent.isascii() and sent.isdigit()


def _hmac(secret: bytes, *parts: bytes) -> bytes:
    mac = hmac.new(secret, digestmod=hashlib.sha256)
    for part in parts:
        mac.update(part)
    return mac.digest()


def _signed(message_id: str, timestamp: str, body: bytes) -> tuple[bytes, bytes]:
    return f"{message_id}.{timestamp}.".encode(), body


def _hex(value: str) -> bytes | None:
    try:
        return bytes.fromhex(value) if value.isascii() and len(value) == 64 else None
    except ValueError:
        return None


class _Malformed(Exception):
    """The headers are missing or malformed: a 400."""


@dataclass(frozen=True, slots=True)
class _Delivery:
    #: The id, None when the sender sends none (a digest of what's signed is used).
    id: str | None
    #: The timestamp as sent (what's signed), None when the sender signs no time (GitHub).
    sent: str | None
    signatures: list[str]


class _Scheme:
    name: str = ""
    #: The header whose length is bounded: the signatures.
    signature_header: str = ""

    def __init__(self, secret: WebhookSecrets) -> None:
        raise NotImplementedError  # pragma: no cover

    def read(self, headers: Mapping[str, str]) -> _Delivery:
        raise NotImplementedError  # pragma: no cover

    def check(self, delivery: _Delivery, body: bytes) -> bool:
        raise NotImplementedError  # pragma: no cover


class _Standard(_Scheme):
    name = "standard"
    signature_header = "webhook-signature"

    def __init__(self, secret: WebhookSecrets) -> None:
        self.keys = [_standard_key(item) for item in _items(secret)]

    def read(self, headers: Mapping[str, str]) -> _Delivery:
        message_id = headers.get("webhook-id")
        sent = headers.get("webhook-timestamp")
        signatures = headers.get("webhook-signature")
        if message_id is None or sent is None or signatures is None:
            raise _Malformed
        if not (_valid_id(message_id) and _timestamp(sent)):
            raise _Malformed
        return _Delivery(message_id, sent, signatures.split(" "))

    def check(self, delivery: _Delivery, body: bytes) -> bool:
        assert delivery.id is not None and delivery.sent is not None  # noqa: S101  # read() did
        signed = _signed(delivery.id, delivery.sent, body)
        expected = [_hmac(key.hmac, *signed) for key in self.keys if key.hmac is not None]
        public = [key.public for key in self.keys if key.public is not None]
        for signature in delivery.signatures:
            version, _, encoded = signature.partition(",")
            if version not in ("v1", "v1a"):
                continue  # another scheme, or a stray space
            try:
                candidate = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                continue
            if version == "v1" and any(hmac.compare_digest(candidate, mac) for mac in expected):
                return True
            if version == "v1a" and any(_ed25519_valid(key, candidate, signed) for key in public):
                return True
        return False


def _ed25519_valid(key: Any, signature: bytes, signed: tuple[bytes, bytes]) -> bool:
    from cryptography.exceptions import InvalidSignature

    try:
        key.verify(signature, b"".join(signed))
    except InvalidSignature:
        return False
    return True


class _GitHub(_Scheme):
    name = "github"
    signature_header = "x-hub-signature-256"

    def __init__(self, secret: WebhookSecrets) -> None:
        self.secrets = [_provider_secret(item) for item in _items(secret)]

    def read(self, headers: Mapping[str, str]) -> _Delivery:
        delivery = headers.get("x-github-delivery")
        signature = headers.get("x-hub-signature-256")
        if delivery is None or signature is None or not _valid_id(delivery):
            raise _Malformed
        return _Delivery(delivery, None, [signature])

    def check(self, delivery: _Delivery, body: bytes) -> bool:
        scheme, _, value = delivery.signatures[0].partition("=")
        candidate = _hex(value) if scheme == "sha256" else None
        return candidate is not None and any(
            hmac.compare_digest(candidate, _hmac(secret, body)) for secret in self.secrets
        )


class _Stripe(_Scheme):
    name = "stripe"
    signature_header = "stripe-signature"

    def __init__(self, secret: WebhookSecrets) -> None:
        # Stripe signs with the secret's string itself, "whsec_" included: not its base64.
        self.secrets = [_provider_secret(item) for item in _items(secret)]

    def read(self, headers: Mapping[str, str]) -> _Delivery:
        header = headers.get("stripe-signature")
        if header is None:
            raise _Malformed
        sent, signatures = None, []
        for item in header.split(","):
            name, _, value = item.strip().partition("=")
            if name == "t":
                sent = value
            elif name == "v1":
                signatures.append(value)  # v0 and others: test mode or other schemes
        if sent is None or not _timestamp(sent) or not signatures:
            raise _Malformed
        return _Delivery(None, sent, signatures)

    def check(self, delivery: _Delivery, body: bytes) -> bool:
        assert delivery.sent is not None  # noqa: S101  # read() checked it
        expected = [_hmac(secret, f"{delivery.sent}.".encode(), body) for secret in self.secrets]
        return any(
            candidate is not None and any(hmac.compare_digest(candidate, mac) for mac in expected)
            for candidate in map(_hex, delivery.signatures)
        )


class _Slack(_Scheme):
    name = "slack"
    signature_header = "x-slack-signature"

    def __init__(self, secret: WebhookSecrets) -> None:
        self.secrets = [_provider_secret(item) for item in _items(secret)]

    def read(self, headers: Mapping[str, str]) -> _Delivery:
        sent = headers.get("x-slack-request-timestamp")
        signature = headers.get("x-slack-signature")
        if sent is None or signature is None or not _timestamp(sent):
            raise _Malformed
        return _Delivery(None, sent, [signature])

    def check(self, delivery: _Delivery, body: bytes) -> bool:
        assert delivery.sent is not None  # noqa: S101  # read() checked it
        version, _, value = delivery.signatures[0].partition("=")
        candidate = _hex(value) if version == "v0" else None
        base = f"v0:{delivery.sent}:".encode()
        return candidate is not None and any(
            hmac.compare_digest(candidate, _hmac(secret, base, body)) for secret in self.secrets
        )


_SCHEMES: dict[str, type[_Scheme]] = {
    "standard": _Standard,
    "github": _GitHub,
    "stripe": _Stripe,
    "slack": _Slack,
}

# What the counted outcome of a refused delivery is, by status.
_REFUSALS = {400: "malformed", 401: "rejected", 413: "too_large"}


@dataclass(frozen=True, slots=True)
class WebhookMessage:
    """A webhook whose signature was checked: what `FastAPIWebhooks` gives the endpoint."""

    #: The sender's id of the message (`webhook-id`, GitHub's delivery id), the same for every
    #: attempt; for Stripe and Slack, which send none, a digest of what was signed (their event
    #: id is in the body).
    id: str
    #: When this attempt was signed, in UTC; None for GitHub, which signs no time.
    timestamp: datetime | None
    #: The raw body, exactly as signed.
    body: bytes

    def json(self) -> Any:
        """The body, parsed as JSON."""
        return json.loads(self.body)


class WebhookSigner:
    """Signs outgoing webhooks the Standard Webhooks way.

        signer = WebhookSigner(settings.webhook_secret)
        body = json.dumps(event).encode()
        await client.post(url, content=body, headers=signer.sign(body))

    Sign the exact bytes you send: `json=event` would serialize them again, maybe differently.

    :param secret: `whsec_<base64>` (`generate_secret()`), the base64 alone, or raw bytes, at
        least 24 bytes once decoded, for HMAC signatures (`v1`); or `whsk_<base64>`, an Ed25519
        private key (`generate_key_pair()`), for `v1a`, which receivers check with the public key
        alone. With a list, every message is signed with each of them, so that receivers that
        have the old secret and those that have the new one both accept it while it's rotated.
    """

    def __init__(self, secret: WebhookSecrets) -> None:
        self._keys = [_standard_key(item) for item in _items(secret)]
        if any(key.hmac is None and key.private is None for key in self._keys):
            raise ValueError("a whpk_ key only verifies: sign with its whsk_ private key")

    @staticmethod
    def generate_secret() -> str:
        """A new random secret (256 bits), as `whsec_<base64>`."""
        return PREFIX + base64.b64encode(secrets.token_bytes(32)).decode()

    @staticmethod
    def generate_key_pair() -> tuple[str, str]:
        """A new Ed25519 key pair for `v1a`: (`whsk_...`, the private key the sender keeps;
        `whpk_...`, the public key receivers verify with)."""
        from cryptography.hazmat.primitives import serialization

        private = _ed25519().Ed25519PrivateKey.generate()
        seed = private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return (
            PRIVATE_PREFIX + base64.b64encode(seed).decode(),
            PUBLIC_PREFIX + base64.b64encode(public).decode(),
        )

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
        signed = _signed(message_id, sent, body)
        signatures = []
        for key in self._keys:
            if key.hmac is not None:
                signatures.append("v1," + base64.b64encode(_hmac(key.hmac, *signed)).decode())
            else:
                signature = key.private.sign(b"".join(signed))
                signatures.append("v1a," + base64.b64encode(signature).decode())
        return {
            "webhook-id": message_id,
            "webhook-timestamp": sent,
            "webhook-signature": " ".join(signatures),
        }


class FastAPIWebhooks:
    """FastAPI dependency that accepts only webhooks signed with `secret`, and each delivery once.

        webhooks = FastAPIWebhooks(settings.webhook_secret)

        @app.post("/webhooks")
        async def receive(message: Annotated[WebhookMessage, Depends(webhooks)]) -> None: ...

    It reads the raw body (up to `max_body_size` bytes, a 413 beyond) and leaves it in the request,
    for `message.json()` or `await request.json()`. Validate it there, not with a pydantic body
    parameter: FastAPI reads and validates those before any dependency, without the size limit and
    before the signature is checked. It can't read a body the endpoint takes as a form, which
    consumes it first (Slack's slash commands come as a form: read `await request.form()` after
    the dependency, from the body it kept).

    Missing or malformed headers get a 400; a wrong signature, a timestamp more than `tolerance`
    seconds away from the server's clock (either way) and a delivery accepted before all get the
    same 401, which doesn't say which check failed.

    Replays: each delivery accepted (its id and timestamp, hashed) is counted in the cache's
    backend until its timestamp can't be accepted anymore, so every process refuses it again. A
    retry of the sender has a new timestamp and signature and goes through: senders keep the id
    of a message for all its attempts, and a retry after a failed (5xx) attempt must get through.
    So make the endpoint idempotent on `message.id` (a unique constraint), if a message must not
    be processed twice. GitHub signs no time: its deliveries are refused again only for the same
    window, and a capture sent later goes through. Whoever can write to the backend can delete the
    marks (and allow a replay within `tolerance`): keep it out of reach.

    :param secret: what the sender signs with. Standard Webhooks: `whsec_<base64>`, the base64
        alone or raw bytes (at least 24 bytes once decoded), or `whpk_<base64>`, the sender's
        Ed25519 public key (`v1a`). GitHub, Stripe (`whsec_...`, used as it is), Slack: the
        string they give, at least 16 characters. A list rotates them: any of them verifies.
    :param scheme: the sender's: "standard" (Standard Webhooks: Svix, Supabase, Clerk, Resend,
        OpenAI...), "github" (`X-Hub-Signature-256`), "stripe" (`Stripe-Signature`), "slack"
        (`X-Slack-Signature`).
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
        scheme: Scheme = "standard",
        tolerance: int = 300,
        cache: Cache | None = None,
        max_body_size: int = 1_048_576,
        namespace: str = "webhook",
        fail_open: bool = False,
    ) -> None:
        if scheme not in _SCHEMES:
            raise ValueError(f"scheme is one of {sorted(_SCHEMES)}, not {scheme!r}")
        if tolerance < 1:
            raise ValueError("tolerance must be at least 1 second")
        if max_body_size < 0:
            raise ValueError("max_body_size can't be negative")
        self.scheme = scheme
        self._scheme = _SCHEMES[scheme](secret)
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
        metrics.count("webhooks", outcome=outcome, scheme=self.scheme)

    def verify(self, headers: Mapping[str, str], body: bytes) -> WebhookMessage:
        """The message, if `headers` carry a valid signature of `body` and a fresh timestamp;
        raises an HTTPException (400 or 401) otherwise. It doesn't check for replays: for tests,
        scripts and queues that deduplicate on their own."""
        lowered = {name.lower(): value for name, value in headers.items()}
        if len(lowered.get(self._scheme.signature_header, "")) > _MAX_SIGNATURES:
            raise HTTPException(401, _INVALID)
        try:
            delivery = self._scheme.read(lowered)
        except _Malformed:
            raise HTTPException(400, _INVALID) from None
        timestamp = None
        if delivery.sent is not None:
            timestamp = int(delivery.sent)
            if abs(time.time() - timestamp) > self.tolerance:
                raise HTTPException(401, _INVALID)
        if not self._scheme.check(delivery, body):
            raise HTTPException(401, _INVALID)
        message_id = delivery.id
        if message_id is None:  # Stripe, Slack: what was signed names the message
            digest = hashlib.sha256(f"{delivery.sent}.".encode() + body).hexdigest()
            message_id = f"sha256:{digest}"
        when = datetime.fromtimestamp(timestamp, UTC) if timestamp is not None else None
        return WebhookMessage(message_id, when, body)

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
        backend = shared_backend(cache.backend)
        if not backend.supports_counters:
            raise NotImplementedError(
                f"{type(backend).__name__} can't keep counters, which FastAPIWebhooks needs"
            )
        sent = int(message.timestamp.timestamp()) if message.timestamp is not None else None
        digest = hashlib.sha256(json.dumps([self.scheme, message.id, sent]).encode()).hexdigest()
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
