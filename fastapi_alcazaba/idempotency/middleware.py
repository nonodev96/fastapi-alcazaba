"""Idempotency keys (draft-ietf-httpapi-idempotency-key-header): a client that retries a POST
after a timeout or a dropped connection gets the first response again, instead of creating a
second order or charging twice.

The client sends `Idempotency-Key: "<a fresh UUID>"`. The first request runs and its response is
stored, with a fingerprint of the request (method, path, query and body); a retry with the same
key and the same request gets that response back with `Idempotent-Replayed: true`. The same key
with a different request is refused with a 422, and a retry while the first request is still
running with a 409: a lock in the backend makes sure that only one of them runs.
"""

import base64
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Collection
from inspect import isawaitable

from fastapi import HTTPException
from starlette.requests import HTTPConnection, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_alcazaba.core.asgi import respond
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.response_cache.caches import Cache, get_cache
from fastapi_alcazaba.telemetry.tracing import set_attributes

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: Who the client is: receives the request and returns a string. Keys are only looked up among the
#: requests of the same client.
Identifier = Callable[[HTTPConnection], Awaitable[str] | str]

HEADER = "idempotency-key"
_MAX_KEY_LENGTH = 255
# Not kept: answers worth retrying, and answers about the request itself (its credentials, route
# or shape), given before the endpoint did anything. A retry gets them afresh, and an anonymous
# client can't fill the backend with them (FastAPI's 422 echoes the request body).
_NOT_KEPT = frozenset({401, 403, 404, 405, 406, 408, 409, 411, 413, 414, 415, 422, 425, 429, 431})


def authorization(connection: HTTPConnection) -> str:
    """The default identifier: the `Authorization` header (hashed before it's stored), so that
    one user never gets another's response, whatever key they send."""
    return connection.headers.get("authorization", "")


def require_idempotency_key(request: Request) -> str:
    """FastAPI dependency for routes that must not run without an `Idempotency-Key`:

        @app.post("/payments", dependencies=[Depends(require_idempotency_key)])

    A request without one gets a 400; it returns the key.
    """
    value = request.headers.get(HEADER)
    if not value:
        raise HTTPException(400, "This request needs an Idempotency-Key header")
    return value


def _parse_key(raw: bytes) -> str | None:
    """The key, from a Structured Field string (`"abc"`, as the draft says) or a bare token
    (`abc`, as most clients send it); None if it isn't a valid key."""
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if not 1 <= len(text) <= _MAX_KEY_LENGTH or not all(" " <= char <= "~" for char in text):
        return None
    return text


class IdempotencyMiddleware:
    """ASGI middleware that makes requests with an `Idempotency-Key` header run once.

        app.add_middleware(IdempotencyMiddleware)

    Requests without the header go through untouched (`require_idempotency_key` makes it
    mandatory on a route). The stored responses go through the cache's signing or encryption:
    use an `encryption_key` when they carry personal data.

    :param cache: where responses and locks are kept; the app's (`Cache.install`) or
        FastAPICache's by default. Its backend must support locks.
    :param methods: the methods it applies to; PUT and DELETE are idempotent already.
    :param expire: seconds a response is kept for retries.
    :param identifier: who the client is (`authorization` by default). Return the user's id
        with cookie sessions, and with tokens that get refreshed: a retry sent with a fresh token
        would run again. Requests without credentials share one space of keys, unguessable as
        long as clients make them random (UUIDs).
    :param max_body_size: larger request bodies get a 413: they're read whole to fingerprint them.
    :param max_response_size: larger responses aren't kept (a retry would run again).
    :param lock_ttl: seconds the lock outlives a process that dies while running the request.
    :param fail_open: when the backend fails, run requests without idempotency instead of
        answering 503. Off by default: running a payment twice is worse than refusing it.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        cache: Cache | None = None,
        methods: Collection[str] = ("POST", "PATCH"),
        expire: int = 86_400,
        identifier: Identifier | None = None,
        max_body_size: int = 1_048_576,
        max_response_size: int = 1_048_576,
        lock_ttl: float = 60.0,
        fail_open: bool = False,
    ) -> None:
        if expire < 1:
            raise ValueError("expire must be at least 1 second")
        if max_body_size < 0 or max_response_size < 0:
            raise ValueError("the sizes can't be negative")
        self.app = app
        self.cache = cache
        self.methods = frozenset(method.upper() for method in methods)
        self.expire = expire
        self.identifier = identifier or authorization
        self.max_body_size = max_body_size
        self.max_response_size = max_response_size
        self.lock_ttl = lock_ttl
        self.fail_open = fail_open

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in self.methods:
            await self.app(scope, receive, send)
            return
        raw = next((value for name, value in scope["headers"] if name == HEADER.encode()), None)
        if raw is None:
            await self.app(scope, receive, send)
            return
        key = _parse_key(raw)
        if key is None:
            self._note(scope, "invalid")
            await respond(scope, send, 400, "Invalid Idempotency-Key header")
            return
        body = await self._read_body(receive)
        if body is None:
            return  # the client went away before sending the whole body: nothing to run
        if len(body) > self.max_body_size:
            self._note(scope, "too_large")
            await respond(scope, send, 413, "Request body too large")
            return
        connection = HTTPConnection(scope)
        cache = self.cache if self.cache is not None else get_cache(connection)
        identity = self.identifier(connection)
        if isawaitable(identity):
            identity = await identity
        digest = hashlib.sha256(json.dumps([str(identity), key]).encode()).hexdigest()[:32]
        record_key = cache.state_key("idempotency", f"{{{digest}}}")
        fingerprint = hashlib.sha256(
            b"\0".join([scope["method"].encode(), scope["path"].encode(), scope["query_string"]])
            + b"\0"
            + body
        ).hexdigest()
        replay = _replay_body(body, receive)
        lock = cache.lock(f"idempotency:{digest}", ttl=self.lock_ttl)
        try:
            if (
                answer := await self._answer_from(cache, record_key, fingerprint, scope, send)
            ) is not None:
                self._note(scope, answer, cache)
                return
            if not await lock.acquire():
                self._note(scope, "conflict", cache)
                await respond(
                    scope,
                    send,
                    409,
                    "A request with this Idempotency-Key is still being processed",
                    {"Retry-After": "1"},
                )
                return
        except BackendUnavailable as exc:
            await self._backend_failed(exc, scope, replay, send, cache)
            return
        try:
            # The first request may have finished between the read and the lock.
            try:
                answer = await self._answer_from(cache, record_key, fingerprint, scope, send)
            except BackendUnavailable as exc:
                await self._backend_failed(exc, scope, replay, send, cache)
                return
            if answer is not None:
                self._note(scope, answer, cache)
                return
            # The app's own exceptions, BackendUnavailable included, aren't ours to answer.
            response, kept = _Recorder(send, self.max_response_size), False
            try:
                await self.app(scope, replay, response.send)
                kept = response.keepable and await self._store(
                    cache, record_key, fingerprint, response
                )
            finally:
                self._note(scope, "stored" if kept else "not_kept", cache)
        finally:
            await lock.release()

    def _note(self, scope: Scope, outcome: str, cache: Cache | None = None) -> None:
        """Count the outcome, and put it on the request's span."""
        set_attributes(idempotency=outcome)
        if cache is None:
            try:
                cache = self.cache if self.cache is not None else get_cache(HTTPConnection(scope))
            except RuntimeError:  # no cache configured: the request fails anyway
                return
        cache.metrics.count("idempotency", outcome=outcome)

    async def _backend_failed(
        self,
        exc: BackendUnavailable,
        scope: Scope,
        receive: Receive,
        send: Send,
        cache: Cache,
    ) -> None:
        logger.warning("Idempotency not checked: the backend failed", exc_info=exc)
        self._note(scope, "unchecked", cache)
        if self.fail_open:
            await self.app(scope, receive, send)
        else:
            await respond(scope, send, 503, "Service Unavailable")

    async def _read_body(self, receive: Receive) -> bytes | None:
        """The request body, None if the client went away before sending all of it. Stops
        reading once it's past max_body_size."""
        chunks, size = [], 0
        while size <= self.max_body_size:
            message = await receive()
            if message["type"] != "http.request":
                return None  # http.disconnect
            chunk = message.get("body", b"")
            chunks.append(chunk)
            size += len(chunk)
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    async def _answer_from(
        self,
        cache: Cache,
        record_key: str,
        fingerprint: str,
        scope: Scope,
        send: Send,
    ) -> str | None:
        """Answer from the stored response, if there is one: "replayed", or "mismatch" when it
        was another request's; None otherwise."""
        stored = await cache.guard.run("get", lambda: cache.backend.get(record_key))
        if stored is None:
            return None
        protector = cache.options.protector
        payload = protector.open(record_key, stored) if protector is not None else stored
        record = _load_record(payload)
        if record is None:
            logger.warning("Ignoring an idempotency record that isn't ours: %r", record_key)
            return None
        stored_fingerprint, status, headers, body = record
        if stored_fingerprint != fingerprint:
            await respond(
                scope, send, 422, "This Idempotency-Key was used with a different request"
            )
            return "mismatch"
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [*headers, (b"idempotent-replayed", b"true")],
            }
        )
        await send({"type": "http.response.body", "body": body})
        return "replayed"

    async def _store(
        self,
        cache: Cache,
        record_key: str,
        fingerprint: str,
        response: "_Recorder",
    ) -> bool:
        record = {
            "fingerprint": fingerprint,
            "status": response.status,
            "headers": [
                [name.decode("latin-1"), value.decode("latin-1")]
                for name, value in response.headers
            ],
            "body": base64.b64encode(b"".join(response.body)).decode(),
        }
        payload = json.dumps(record).encode()
        protector = cache.options.protector
        stored = protector.seal(record_key, payload) if protector is not None else payload
        try:
            await cache.guard.run("set", lambda: cache.backend.set(record_key, stored, self.expire))
        except BackendUnavailable as exc:
            # The request ran; a retry would run it again.
            logger.warning("Couldn't keep the response for its Idempotency-Key", exc_info=exc)
            return False
        return True


class _Recorder:
    """Passes the response on to the client and keeps a copy."""

    def __init__(self, send: Send, max_size: int) -> None:
        self._send = send
        self._max_size = max_size
        self.status = 0
        self.headers: list[tuple[bytes, bytes]] = []
        self.body: list[bytes] = []
        self._size = 0
        self._complete = False

    @property
    def keepable(self) -> bool:
        """A whole response that a retry should get again: not a server error, a "try again"
        answer or one about the request itself, and not too large."""
        return (
            self._complete
            and self.status < 500
            and self.status not in _NOT_KEPT
            and self._size <= self._max_size
        )

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            self._size += len(chunk)
            if self._size <= self._max_size:
                self.body.append(chunk)
            self._complete = not message.get("more_body", False)
        await self._send(message)


def _load_record(
    payload: bytes | None,
) -> tuple[str, int, list[tuple[bytes, bytes]], bytes] | None:
    """(fingerprint, status, headers, body) from a stored record; None if it isn't one."""
    try:
        record = json.loads(payload) if payload is not None else None
        if not isinstance(record, dict):
            return None
        fingerprint, status = record["fingerprint"], record["status"]
        if not isinstance(fingerprint, str) or type(status) is not int:
            return None
        headers = [
            (name.encode("latin-1"), value.encode("latin-1")) for name, value in record["headers"]
        ]
        return fingerprint, status, headers, base64.b64decode(record["body"], validate=True)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _replay_body(body: bytes, receive: Receive) -> Receive:
    """A receive that gives the app the body read already, then what the server sends."""
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay
