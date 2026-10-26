"""ASGI middlewares for what FastAPI leaves to the deployment: the security headers of every
response, and a limit on the size of request bodies.

Both are plain ASGI middlewares, not `BaseHTTPMiddleware` (which buffers responses and breaks
streaming), and only touch HTTP requests: WebSockets and lifespan events go through as they are.
"""

import json
import re
from collections.abc import Mapping

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Literal codes: the name of 413 in starlette.status changed with its RFC 9110 title.
_BAD_REQUEST = 400
_CONTENT_TOO_LARGE = 413
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
# Where in the request's state the rate limiters leave their headers, for the error responses
# that FastAPIProblemDetails builds (FastAPI doesn't give them the dependencies' headers).
_RATE_LIMIT_HEADERS = "fastapi_alcazaba_rate_limit_headers"
_FORBIDDEN_IN_VALUES = re.compile(r"[\r\n\0]")

#: The OWASP REST Security Cheat Sheet's headers for API responses.
DEFAULT_SECURITY_HEADERS: Mapping[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}
_HSTS = b"strict-transport-security"
_CSP = b"content-security-policy"


def _header(name: str, value: str) -> tuple[bytes, bytes]:
    """The header as ASGI wants it, refusing what would split it into two (CR, LF)."""
    if not _TOKEN.fullmatch(name):
        raise ValueError(f"invalid header name: {name!r}")
    if _FORBIDDEN_IN_VALUES.search(value):
        raise ValueError(f"invalid value for the {name} header: {value!r}")
    return name.lower().encode("latin-1"), value.encode("latin-1")


class SecurityHeadersMiddleware:
    """Adds the security headers of the OWASP REST Security Cheat Sheet to every response,
    without overwriting the ones the app sets itself.

        app.add_middleware(SecurityHeadersMiddleware)

    - `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`
      and `Cross-Origin-Opener-Policy: same-origin` on every response.
    - `Strict-Transport-Security` only on HTTPS requests: on plain HTTP browsers ignore it, and on
      a development `localhost` it would stick. Behind a proxy that terminates TLS, tell the server
      to trust it (`uvicorn --proxy-headers --forwarded-allow-ips=<proxy address>`) so that the
      request's scheme is https.
    - `Content-Security-Policy`: `csp` for responses that aren't HTML (nothing in a JSON response
      should load anything), `html_csp` for HTML ones. None by default, so that FastAPI's Swagger
      UI and ReDoc, which load scripts from a CDN, keep working.

    :param headers: more headers, or other values for the defaults; None removes a default.
    :param hsts: the `Strict-Transport-Security` value; None leaves it out.
    :param csp: the `Content-Security-Policy` of responses that aren't HTML; None leaves it out.
    :param html_csp: the `Content-Security-Policy` of HTML responses.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        headers: Mapping[str, str | None] | None = None,
        hsts: str | None = "max-age=63072000; includeSubDomains",
        csp: str | None = "default-src 'none'; frame-ancestors 'none'",
        html_csp: str | None = None,
    ) -> None:
        self.app = app
        values: dict[str, str | None] = {k.lower(): v for k, v in DEFAULT_SECURITY_HEADERS.items()}
        for name, value in (headers or {}).items():
            if name.lower().encode() in (_HSTS, _CSP):
                # One way to set each: csp/html_csp tell HTML from the rest, hsts checks HTTPS.
                raise ValueError(f"set {name} with the hsts, csp or html_csp arguments")
            values[name.lower()] = value
        self._headers = [_header(name, value) for name, value in values.items() if value]
        self._hsts = _header("Strict-Transport-Security", hsts)[1] if hsts is not None else None
        self._csp = _header("Content-Security-Policy", csp)[1] if csp is not None else None
        self._html_csp = (
            _header("Content-Security-Policy", html_csp)[1] if html_csp is not None else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = scope.get("scheme") == "https"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = self._complete(list(message.get("headers", [])), https)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _complete(
        self,
        headers: list[tuple[bytes, bytes]],
        https: bool,
    ) -> list[tuple[bytes, bytes]]:
        present = {name.lower() for name, _ in headers}
        content_type = next((v for n, v in headers if n.lower() == b"content-type"), b"")
        html = content_type.split(b";")[0].strip().lower() == b"text/html"
        extra = list(self._headers)
        if https and self._hsts is not None:
            extra.append((_HSTS, self._hsts))
        csp = self._html_csp if html else self._csp
        if csp is not None:
            extra.append((_CSP, csp))
        return [*headers, *((name, value) for name, value in extra if name not in present)]


class _BodyTooLarge(HTTPException):
    """Raised from `receive`. An HTTPException, so that FastAPI doesn't turn it into a 400 while
    it parses the body, and the app's exception handlers answer the 413."""

    def __init__(self) -> None:
        super().__init__(_CONTENT_TOO_LARGE, "Request body too large")


def _route_path(scope: Scope) -> str:
    """The path the app routes, without the `root_path` a proxy or a mount adds in front."""
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        if path == root_path:
            return "/"
        if path[len(root_path)] == "/":
            return path[len(root_path) :]
    return path


async def _respond(
    scope: Scope,
    send: Send,
    status: int,
    detail: str,
    headers: Mapping[str, str] | None = None,
) -> None:
    """A middleware's own answer: a problem when the app has FastAPIProblemDetails installed (so
    that every error of the app looks the same), else FastAPI's `{"detail": ...}`."""
    problems = getattr(getattr(scope.get("app"), "state", None), "fastapi_alcazaba_problems", None)
    if problems is not None:
        from fastapi_alcazaba.problems import Problem

        # A Problem: its detail is sent, also with a 5xx, as it's written for the client.
        response = problems.response(Request(scope), Problem(status, detail, headers=headers))
        await response(scope, _disconnected, send)
        return
    body = json.dumps({"detail": detail}, separators=(",", ":")).encode()
    raw = [(b"content-type", b"application/json"), (b"content-length", b"%d" % len(body))]
    raw += [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


async def _disconnected() -> Message:
    """The receive of an answer that reads nothing."""
    return {"type": "http.disconnect"}


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
        limit = self.limit(_route_path(scope))
        if limit is None:
            await self.app(scope, receive, send)
            return
        lengths = {value for name, value in scope["headers"] if name.lower() == b"content-length"}
        if lengths:
            # Digits only: int() would also take "+5", " 5" or "1_0". Several different values
            # make it ambiguous which one the body follows (request smuggling).
            if len(lengths) > 1 or not (length := lengths.pop()).isdigit():
                await _respond(scope, send, _BAD_REQUEST, "Invalid Content-Length")
                return
            # int() refuses more than 4300 digits; a length with more than the limit is over it.
            digits = length.lstrip(b"0")
            if len(digits) > len(str(limit)) or int(digits or b"0") > limit:
                await _respond(scope, send, _CONTENT_TOO_LARGE, "Request body too large")
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
            await _respond(scope, send, _CONTENT_TOO_LARGE, "Request body too large")
