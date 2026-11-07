"""Sending webhooks to the URLs an app's users register: signed the Standard Webhooks way, through
the SSRF guard, retried by the task queue until delivered, at least once:
pip install "fastapi-alcazaba[webhooks]".

    async def endpoint(endpoint_id: str) -> WebhookEndpoint | None:
        row = await db.get(Endpoint, endpoint_id)  # the app's table: URL, secret
        return WebhookEndpoint(row.url, row.secret) if row else None

    sender = WebhookSender(queue, endpoint)

    # In the outbox's relay: written with the change, sent once it's committed.
    async def deliver(message: OutboxMessage) -> None:
        payload = message.payload
        await sender.send(payload["endpoint"], payload["event"], message_id=message.id)

Each delivery is a task of the queue, enqueued once per endpoint and message: the receiver gets
the same `webhook-id` at every attempt, and deduplicates on it. An endpoint that keeps failing is
disabled; `enable()` turns it back on.
"""

import hashlib
import inspect
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.background.tasks import FastAPITaskQueue, current_task
from fastapi_alcazaba.outbound.transports import ResilientTransport, SSRFGuardTransport
from fastapi_alcazaba.response_cache.caches import Cache, resolve_cache
from fastapi_alcazaba.webhooks.signing import WebhookSecrets, WebhookSigner

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover  # httpx under its old name
    try:
        import httpx  # type: ignore[no-redef,import-not-found]
    except ImportError as exc:
        raise ImportError(
            'WebhookSender needs httpx2: pip install "fastapi-alcazaba[webhooks]"'
        ) from exc

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: The task the sender registers on the queue.
TASK = "fastapi_alcazaba.webhooks.deliver"
#: Bytes of a receiver's answer read at most: it's only looked at for its status.
MAX_RESPONSE = 64 * 1024
#: The states of a delivery.
STATES = ("retrying", "succeeded", "failed", "skipped")


class WebhookDeliveryFailed(Exception):
    """An attempt that the receiver refused or that didn't get there: the queue tries again."""


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    """Where a user wants webhooks, as the app keeps it.

    :param url: an `https://` URL (or `http://` to a host `allow` lets through).
    :param secret: the endpoint's `whsec_...` secret (`WebhookSigner.generate_secret()`), or an
        Ed25519 `whsk_...` key; a list signs with each while it's rotated.
    """

    url: str
    secret: WebhookSecrets


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    """A message's delivery to an endpoint: never its body nor the endpoint's secret."""

    endpoint_id: str
    message_id: str
    #: retrying (failed, tried again later), succeeded, failed (its retries spent: in the queue's
    #: dead letters; or its endpoint disabled by it), skipped (its endpoint disabled or gone).
    state: str
    #: Attempts made (counted again from 1 when it's requeued from the dead letters).
    attempts: int
    #: The status of the last answer; None when none came (a timeout, a refused address).
    status_code: int | None
    #: The type of the last error: `http_<status>`, or an exception's.
    error: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EndpointHealth:
    """Whether an endpoint gets deliveries."""

    endpoint_id: str
    #: Failed attempts in a row.
    failures: int
    disabled: bool


EndpointLookup = Callable[[str], Awaitable[WebhookEndpoint | None] | WebhookEndpoint | None]


class WebhookSender:
    """Sends webhooks through `queue`, registering its delivery task there: create it before the
    queue starts.

    :param queue: the task queue that delivers them (its workers send them).
    :param endpoints: `endpoints(endpoint_id)`, sync or async: the `WebhookEndpoint`, or None
        when it's gone (its deliveries are skipped). Asked at each attempt, so a URL or a secret
        changed is used at once, and no secret travels in the queue.
    :param cache: whose backend keeps the delivery records and the endpoints' failures; the
        queue's, or FastAPICache's.
    :param client: the httpx2 client to send with; by default one whose transport refuses
        private, loopback and metadata addresses (`SSRFGuardTransport`) and bounds each attempt
        (`ResilientTransport`, without a circuit breaker: endpoints of different users may share
        a host, and each one's failures are its own). A client given replaces both.
    :param allow: hosts or networks the SSRF guard lets through although they aren't public (an
        internal receiver; `localhost` in development).
    :param timeout: seconds for each attempt, the answer included.
    :param retries: attempts after the first, with exponential backoff and jitter from `backoff`
        seconds up to `max_backoff`.
    :param disable_after: failed attempts in a row after which the endpoint is disabled; a 410
        Gone disables it at once.
    :param record_ttl: seconds a delivery's record is kept after its last change.
    :param namespace: part of the keys, after the cache's prefix.
    """

    def __init__(
        self,
        queue: FastAPITaskQueue,
        endpoints: EndpointLookup,
        *,
        cache: Cache | None = None,
        client: "httpx.AsyncClient | None" = None,
        allow: Iterable[str] = (),
        timeout: float = 10.0,
        retries: int = 8,
        backoff: float = 5.0,
        max_backoff: float = 6 * 3600,
        disable_after: int = 20,
        record_ttl: int = 7 * 24 * 3600,
        namespace: str = "webhook_sender",
    ) -> None:
        if timeout <= 0 or record_ttl < 1 or disable_after < 1:
            raise ValueError("timeout and record_ttl must be positive; disable_after at least 1")
        self.queue = queue
        self.endpoints = endpoints
        self.cache = cache
        self.timeout = timeout
        self.retries = retries
        self.disable_after = disable_after
        self.record_ttl = record_ttl
        self.namespace = namespace
        self._own_client = client is None
        self.client = (
            client
            if client is not None
            else httpx.AsyncClient(
                transport=ResilientTransport(
                    SSRFGuardTransport(allow=allow),
                    timeout=timeout,
                    # No breaker per origin: one user's endpoint that fails would refuse the
                    # others' on the same host, and count against them. Each endpoint's own
                    # failures disable it (`disable_after`).
                    failure_threshold=None,
                    max_response_size=MAX_RESPONSE,
                ),
                follow_redirects=False,
            )
        )
        # The queue's timeout is a backstop: the transport's deadline comes first.
        queue.task(
            TASK,
            retries=retries,
            backoff=backoff,
            max_backoff=max_backoff,
            timeout=timeout * 3,
            retry_on=(WebhookDeliveryFailed,),
        )(self._deliver)

    async def send(self, endpoint_id: str, payload: Any, *, message_id: str | None = None) -> str:
        """Deliver `payload` (JSON) to the endpoint, through the queue; returns the message id.
        Give the same `message_id` (the outbox message's) when the same message may be sent
        again: while its delivery to that endpoint is pending, it isn't enqueued twice, and the
        receiver gets the same `webhook-id`."""
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise ValueError("endpoint_id is a non-empty string")
        message_id = message_id or f"msg_{secrets.token_hex(16)}"
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        await self.queue.enqueue(
            TASK, endpoint_id, message_id, body, unique_key=f"{endpoint_id}\x00{message_id}"
        )
        return message_id

    async def delivery(self, endpoint_id: str, message_id: str) -> WebhookDelivery | None:
        """The record of the message's delivery to the endpoint, as of its last attempt; None
        before its first attempt (or once it expired)."""
        cache = self._cache()
        key = self._key("delivery", endpoint_id, message_id)
        raw = await cache.guard.run("get", lambda: shared_backend(cache.backend).get(key))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return WebhookDelivery(
                endpoint_id=endpoint_id,
                message_id=message_id,
                state=str(data["state"]),
                attempts=int(data["attempts"]),
                status_code=data.get("status_code"),
                error=data.get("error"),
                updated_at=datetime.fromtimestamp(float(data["updated"]), UTC),
            )
        except (ValueError, KeyError, TypeError):
            return None  # not ours: whoever can write to the backend wrote it

    async def health(self, endpoint_id: str) -> EndpointHealth:
        """The endpoint's failures in a row, and whether it's disabled."""
        cache = self._cache()
        failures_key = self._key("failures", endpoint_id)
        disabled_key = self._key("disabled", endpoint_id)
        failures = await cache.guard.run(
            "get", lambda: shared_backend(cache.backend).get_counter(failures_key)
        )
        disabled = await cache.guard.run(
            "get", lambda: shared_backend(cache.backend).get(disabled_key)
        )
        return EndpointHealth(endpoint_id, int(failures), disabled is not None)

    async def enable(self, endpoint_id: str) -> None:
        """Deliver to the endpoint again, its failures forgotten (deliveries skipped meanwhile
        aren't sent: send them again)."""
        cache = self._cache()
        for kind in ("failures", "disabled"):
            await self._delete(cache, self._key(kind, endpoint_id))
        logger.info("Webhook endpoint %s enabled", endpoint_id)

    async def disable(self, endpoint_id: str) -> None:
        """Stop delivering to the endpoint: its pending deliveries are skipped."""
        cache = self._cache()
        key = self._key("disabled", endpoint_id)
        stamp = str(time.time()).encode()
        await cache.guard.run("set", lambda: shared_backend(cache.backend).set(key, stamp, None))

    async def aclose(self) -> None:
        """Closes the client it made (not one it was given): in the lifespan, at shutdown."""
        if self._own_client:
            await self.client.aclose()

    # The task.

    async def _deliver(self, endpoint_id: str, message_id: str, body: str) -> None:
        metrics = self._cache().metrics
        found = self.endpoints(endpoint_id)
        endpoint = await found if inspect.isawaitable(found) else found
        if endpoint is None or (await self.health(endpoint_id)).disabled:
            reason = "gone" if endpoint is None else "disabled"
            logger.info(
                "Webhook %s to %s skipped: its endpoint is %s", message_id, endpoint_id, reason
            )
            await self._record(endpoint_id, message_id, "skipped", error=f"endpoint_{reason}")
            metrics.count("webhooks.deliveries", outcome="skipped")
            return
        # Written by this run only: the queue runs one delivery of a message to an endpoint at a
        # time (its unique key), and counts the attempts.
        running = current_task()
        attempts, limit = (running.attempt, running.attempts) if running else (1, self.retries + 1)
        status: int | None = None
        try:
            headers = WebhookSigner(endpoint.secret).sign(body, message_id=message_id)
            headers["content-type"] = "application/json"
            response = await self.client.post(endpoint.url, content=body, headers=headers)
            status = response.status_code
            error = None if 200 <= status < 300 else f"http_{status}"
        except httpx.HTTPError as exc:  # refused address, timeout, breaker open, too large
            error = type(exc).__name__
        if error is None:
            await self._succeeded(endpoint_id, message_id, attempts, status)
            metrics.count("webhooks.deliveries", outcome="delivered")
            return
        disabled = await self._failed(endpoint_id, gone=status == 410)
        final = disabled or attempts >= limit
        state = "failed" if final else "retrying"
        await self._record(
            endpoint_id, message_id, state, attempts=attempts, status_code=status, error=error
        )
        metrics.count("webhooks.deliveries", outcome="disabled" if disabled else state)
        logger.warning(
            "Webhook %s to %s failed (%s), attempt %d%s",
            message_id,
            endpoint_id,
            error,
            attempts,
            ": its endpoint is now disabled" if disabled else "",
        )
        if not disabled:
            # Retried by the queue; the last time, it goes to its dead letters, where
            # `requeue_dead_letters()` (or `fastapi-alcazaba tasks ... requeue`) sends it again.
            raise WebhookDeliveryFailed(error)

    async def _succeeded(
        self,
        endpoint_id: str,
        message_id: str,
        attempts: int,
        status: int | None,
    ) -> None:
        await self._delete(self._cache(), self._key("failures", endpoint_id))
        await self._record(
            endpoint_id, message_id, "succeeded", attempts=attempts, status_code=status
        )

    async def _failed(self, endpoint_id: str, *, gone: bool) -> bool:
        """Count a failed attempt; whether the endpoint got disabled by it."""
        cache = self._cache()
        key = self._key("failures", endpoint_id)
        failures = await cache.guard.run(
            "incr", lambda: shared_backend(cache.backend).incr(key, 1, self.record_ttl)
        )
        if gone or failures >= self.disable_after:
            await self.disable(endpoint_id)
            return True
        return False

    async def _record(
        self,
        endpoint_id: str,
        message_id: str,
        state: str,
        *,
        attempts: int | None = None,
        status_code: int | None = None,
        error: str | None = None,
    ) -> None:
        if attempts is None:
            running = current_task()
            attempts = running.attempt if running is not None else 0
        record = {
            "state": state,
            "attempts": attempts,
            "status_code": status_code,
            "error": error,
            "updated": time.time(),
        }
        cache = self._cache()
        key = self._key("delivery", endpoint_id, message_id)
        value = json.dumps(record).encode()
        await cache.guard.run(
            "set", lambda: shared_backend(cache.backend).set(key, value, self.record_ttl)
        )

    @staticmethod
    async def _delete(cache: Cache, key: str) -> None:
        await cache.guard.run("delete", lambda: shared_backend(cache.backend).clear(key=key))

    # Records and endpoint states are read and written in the shared backend: a TieredBackend's
    # copy in this process would be another process's old news.
    def _cache(self) -> Cache:
        if self.cache is not None:
            return self.cache
        return self.queue.cache if self.queue.cache is not None else resolve_cache(None)

    def _key(self, kind: str, *parts: str) -> str:
        digest = hashlib.sha256("\x00".join(parts).encode()).hexdigest()
        return self._cache().state_key(self.namespace, f"{kind}:{digest}")


__all__ = [
    "EndpointHealth",
    "WebhookDelivery",
    "WebhookDeliveryFailed",
    "WebhookEndpoint",
    "WebhookSender",
]
