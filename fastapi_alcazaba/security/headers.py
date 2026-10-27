"""The security headers of every response: those of the OWASP REST Security Cheat Sheet.

A plain ASGI middleware, not `BaseHTTPMiddleware` (which buffers responses and breaks streaming),
that only touches HTTP requests: WebSockets and lifespan events go through as they are.
"""

import re
from collections.abc import Mapping

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_alcazaba.core.asgi import TOKEN

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
    if not TOKEN.fullmatch(name):
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
