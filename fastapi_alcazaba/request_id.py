"""A request ID for every request, to find all the log lines of one request and to let a client
quote it when it reports an error.

    app.add_middleware(RequestIDMiddleware)  # last: the outermost

    handler.addFilter(RequestIDLogFilter())  # then "%(request_id)s" in the format

`RequestIDMiddleware` takes the ID from the request's `X-Request-ID` (set by a proxy or the
client) when it's safe to log, else makes a random one; `request_id()` returns it anywhere while
the request is handled, and the response carries it in the same header. An incoming ID is
reused only if it matches `[A-Za-z0-9._:-]{1,128}`: anything else (line breaks that would forge
log lines, an endless value) gets a new one.
"""

import logging
import re
import secrets
from contextvars import ContextVar

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_alcazaba.cache import metrics_of
from fastapi_alcazaba.middleware import _TOKEN

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: The incoming IDs that are reused: nothing that could split a log line or a header.
SAFE_REQUEST_ID = re.compile(rb"[A-Za-z0-9._:-]{1,128}")

# (header name, ID) of the request being handled.
_current: ContextVar[tuple[str, str] | None] = ContextVar(
    "fastapi_alcazaba_request_id", default=None
)

_RESPONSE_STARTS = frozenset(
    {"http.response.start", "websocket.accept", "websocket.http.response.start"}
)


def request_id() -> str | None:
    """The ID of the request being handled; None outside a request, or without the middleware."""
    current = _current.get()
    return current[1] if current is not None else None


def _current_header() -> tuple[str, str] | None:
    """(header name, ID), for the error handler that answers outside every middleware."""
    return _current.get()


def _new_id() -> str:
    return secrets.token_hex(16)  # 128 random bits


class RequestIDMiddleware:
    """Gives every HTTP request and WebSocket an ID, available from `request_id()` while it's
    handled and sent back in the response's `header`.

        app.add_middleware(RequestIDMiddleware, trust_incoming=False)

    Add it last, the outermost, so that every log line and every answer of the other middlewares
    carry the ID. When the app raises, the ID stays set for the rest of the request's task: the
    error handler and the server log the exception outside every middleware.

    :param header: the request and response header with the ID.
    :param trust_incoming: reuse the ID in the request's header (when safe; a single value). Set
        it to False when clients reach the app without a proxy that sets the header itself and
        you don't want them to choose their IDs.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        header: str = "X-Request-ID",
        trust_incoming: bool = True,
    ) -> None:
        if not _TOKEN.fullmatch(header):
            raise ValueError(f"invalid header name: {header!r}")
        self.app = app
        self.header = header
        self.trust_incoming = trust_incoming
        self._name = header.lower().encode("latin-1")

    def _incoming(self, scope: Scope) -> tuple[str | None, str]:
        """The incoming ID when it can be reused, and where the request's ID comes from."""
        values: list[bytes] = [v for name, v in scope["headers"] if name.lower() == self._name]
        if not self.trust_incoming or not values:
            return None, "generated"
        # Several values: it's ambiguous which one a proxy set.
        if len(values) == 1 and SAFE_REQUEST_ID.fullmatch(values[0]):
            return values[0].decode("ascii"), "incoming"
        return None, "rejected"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        incoming, source = self._incoming(scope)
        metrics_of(HTTPConnection(scope)).count("request_ids", source=source)
        value = incoming or _new_id()
        encoded = value.encode("ascii")

        async def send_with_id(message: Message) -> None:
            if message["type"] in _RESPONSE_STARTS:
                headers = list(message.get("headers", []))
                if all(name.lower() != self._name for name, _ in headers):
                    message = {**message, "headers": [*headers, (self._name, encoded)]}
            await send(message)

        token = _current.set((self.header, value))
        await self.app(scope, receive, send_with_id)
        # Not in a `finally`: after an exception, Starlette's 500 handler and the server's error
        # log run outside every middleware, in this same task, and still find the ID.
        _current.reset(token)


class RequestIDLogFilter(logging.Filter):
    """Sets `record.request_id` to the ID of the request being handled (`default` outside one),
    for formats with `%(request_id)s`. Add it to the handlers: a logger's filters don't see the
    records of its child loggers.

        handler.addFilter(RequestIDLogFilter())

    A `request_id` passed in `extra=` is kept.
    """

    def __init__(self, *, default: str = "-") -> None:
        super().__init__()
        self.default = default

    def filter(self, record: logging.LogRecord) -> bool:
        if record.__dict__.get("request_id") is None:
            record.__dict__["request_id"] = request_id() or self.default
        return True
