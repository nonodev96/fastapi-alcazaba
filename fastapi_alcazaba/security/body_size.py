"""A limit on the size of request bodies, which FastAPI doesn't have.

A plain ASGI middleware, not `BaseHTTPMiddleware` (which buffers responses and breaks streaming),
that only touches HTTP requests: WebSockets and lifespan events go through as they are.
"""

from collections.abc import Mapping

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_alcazaba.core.asgi import respond, route_path

# Literal codes: the name of 413 in starlette.status changed with its RFC 9110 title.
_BAD_REQUEST = 400
_CONTENT_TOO_LARGE = 413


class _BodyTooLarge(HTTPException):
    """Raised from `receive`. An HTTPException, so that FastAPI doesn't turn it into a 400 while
    it parses the body, and the app's exception handlers answer the 413."""

    def __init__(self) -> None:
        super().__init__(_CONTENT_TOO_LARGE, "Request body too large")


class BodySizeLimitMiddleware:
    """Refuses request bodies larger than `max_size` bytes with a 413. FastAPI has no limit of its
    own: a client could send an endless body that the app reads into memory.

        app.add_middleware(BodySizeLimitMiddleware, max_size=1_048_576, paths={"/uploads": None})

    A `Content-Length` over the limit is refused before the app runs. Bodies without one (chunked)
    are counted while the app reads them, and refused as soon as they pass the limit.

    :param max_size: the limit, in bytes.
    :param paths: other limits for the paths starting with these prefixes (the longest matching
        prefix wins; None for no limit). They're matched like the app's routes: without the
        `root_path` a proxy or a mount adds in front.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_size: int = 1_048_576,
        paths: Mapping[str, int | None] | None = None,
    ) -> None:
        if max_size < 0:
            raise ValueError("max_size can't be negative")
        for prefix, limit in (paths or {}).items():
            if not prefix.startswith("/"):
                raise ValueError(f"path prefixes start with /: {prefix!r}")
            if limit is not None and limit < 0:
                raise ValueError(f"the limit of {prefix} can't be negative")
        self.app = app
        self.max_size = max_size
        self._paths = sorted((paths or {}).items(), key=lambda item: len(item[0]), reverse=True)

    def limit(self, path: str) -> int | None:
        """The limit for requests to `path`; None when there's none."""
        for prefix, limit in self._paths:
            base = prefix.rstrip("/")
            if path == base or path.startswith(base + "/"):
                return limit
        return self.max_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit(route_path(scope))
        if limit is None:
            await self.app(scope, receive, send)
            return
        lengths = {value for name, value in scope["headers"] if name.lower() == b"content-length"}
        if lengths:
            # Digits only: int() would also take "+5", " 5" or "1_0". Several different values
            # make it ambiguous which one the body follows (request smuggling).
            if len(lengths) > 1 or not (length := lengths.pop()).isdigit():
                await respond(scope, send, _BAD_REQUEST, "Invalid Content-Length")
                return
            # int() refuses more than 4300 digits; a length with more than the limit is over it.
            digits = length.lstrip(b"0")
            if len(digits) > len(str(limit)) or int(digits or b"0") > limit:
                await respond(scope, send, _CONTENT_TOO_LARGE, "Request body too large")
                return
        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                # Counted even with a Content-Length: the client may send more than it said.
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _BodyTooLarge:
            if started:
                raise  # too late to answer: the server closes the connection
            await respond(scope, send, _CONTENT_TOO_LARGE, "Request body too large")
