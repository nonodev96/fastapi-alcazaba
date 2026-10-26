"""Protection against cross-site request forgery (CSRF): a page on another site can't make the
user's browser change anything here with the cookies (or HTTP authentication) it sends by itself.

    app.add_middleware(CSRFMiddleware, trusted_origins=["https://admin.example.com"])

Browsers say where a request comes from. Every current one sends `Sec-Fetch-Site`: requests
marked `same-origin` (or `none`, typed by the user) go through, `same-site` and `cross-site` ones
are refused unless their `Origin` is trusted. Older browsers send `Origin` (or at least `Referer`),
which must then be the app's own origin or a trusted one; a request with neither is refused. GET,
HEAD, OPTIONS and TRACE always go through: they mustn't change anything.

A request without cookies or HTTP authentication goes through: whoever sent it, it can only do
what an anonymous client could, and APIs that take tokens in a header aren't exposed to CSRF (a
page on another site can't set the header). Except form submissions from other sites
(`Sec-Fetch-Mode` other than `cors`), which would log the user into the attacker's account (login
CSRF).

`require_token=True` adds a double-submit token for single-page apps: a random token in a cookie
the page's JavaScript reads and sends back in a header, compared in constant time.

WebSocket handshakes are checked too: browsers don't apply CORS to them, so without this any site
could open a WebSocket with the user's cookies and talk to it (cross-site WebSocket hijacking).
"""

import hmac
import logging
import re
import secrets
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_alcazaba._cookies import CookieSettings, cookie_values, has_cookies
from fastapi_alcazaba.cache import metrics_of
from fastapi_alcazaba.middleware import _respond, _route_path
from fastapi_alcazaba.tracing import add_event

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: Methods that mustn't change anything, never checked.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# HTTP authentication the browser remembers and sends by itself, like cookies.
_AMBIENT_SCHEMES = frozenset({b"basic", b"digest", b"negotiate", b"ntlm"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_WEBSOCKET_SCHEMES = {"ws": "http", "wss": "https"}
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32)
_FORBIDDEN = 403
_POLICY_VIOLATION = 1008
_NO_SOURCE = ("no_source", "neither Origin nor Referer")
_INVALID = "null"  # an origin that matches nothing


def _origin(value: str, *, strict: bool) -> str | None:
    """`scheme://host[:port]` of an http(s) URL, lowercased and without the default port; None if
    it isn't one. `strict` refuses anything after the host, as in an `Origin` header."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme, host = parts.scheme.lower(), parts.hostname
    if scheme not in _DEFAULT_PORTS or not host or "@" in parts.netloc:
        return None
    if strict and (parts.path or parts.query or parts.fragment):
        return None
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _one(found: dict[bytes, list[bytes]], name: bytes) -> str | None:
    """The header's value; repeated ones are joined, so that they match nothing (fail closed)."""
    values = found.get(name)
    if not values:
        return None
    return ", ".join(value.decode("latin-1") for value in values)


def _shown(value: str) -> str:
    return repr(value[:100])


class CSRFMiddleware:
    """ASGI middleware that refuses, with a 403, unsafe requests that a page on another site made
    the user's browser send. See the module's documentation for the rules.

        app.add_middleware(CSRFMiddleware)

    The app's own origin is the scheme and the `Host` header as the server sees them. Behind a
    proxy that terminates TLS, let the server trust it (`uvicorn --proxy-headers
    --forwarded-allow-ips=<proxy address>`) so that the scheme is https, and have the proxy pass
    the `Host` header on; otherwise list the public origin in `trusted_origins`.

    :param trusted_origins: other origins (`https://admin.example.com`) whose pages may send
        requests: sibling subdomains count as other origins, and so do identity providers and
        payment pages that post a form back to the app.
    :param exempt_paths: path prefixes that aren't checked (webhooks signed in their own way).
        They're matched like the app's routes: without the `root_path` a proxy or a mount adds.
    :param require_token: also require, on requests with cookies, a header (`header_name`) equal
        to the token cookie (`cookie_name`) set on every response that lacks a valid one. The token
        is in `request.state.csrf_token` too. A request whose browser sends neither Origin nor
        Referer passes with a valid token.
    :param check_all: also check requests without cookies or HTTP authentication: for apps that
        trust TLS client certificates or the network (an intranet), which the browser sends too.
    :param cookie_name: the token cookie. The `__Host-` prefix keeps sibling subdomains from
        setting one (cookie tossing); it needs `cookie_secure` and no `cookie_domain`.
    :param header_name: the header that carries the token.
    :param cookie_secure: only send the token cookie over HTTPS.
    :param cookie_domain: the token cookie's domain, for pages on another subdomain that need to
        read it (then pick a name without the `__Host-` prefix).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        trusted_origins: Iterable[str] = (),
        exempt_paths: Iterable[str] = (),
        require_token: bool = False,
        check_all: bool = False,
        cookie_name: str = "__Host-csrftoken",
        header_name: str = "X-CSRF-Token",
        cookie_secure: bool = True,
        cookie_domain: str | None = None,
    ) -> None:
        origins = set()
        for value in trusted_origins:
            origin = _origin(value.rstrip("/"), strict=True)
            if origin is None:
                raise ValueError(f"trusted origins are http(s)://host[:port], not {value!r}")
            origins.add(origin)
        exempt = []
        for prefix in exempt_paths:
            if not prefix.startswith("/"):
                raise ValueError(f"path prefixes start with /: {prefix!r}")
            exempt.append(prefix.rstrip("/"))
        if not _HEADER_NAME.fullmatch(header_name):
            raise ValueError(f"invalid header name: {header_name!r}")
        self.app = app
        self.trusted_origins = frozenset(origins)
        self.check_all = check_all
        self._exempt = tuple(exempt)
        self._header = header_name.lower().encode("latin-1")
        self._read = frozenset(
            {b"origin", b"referer", b"sec-fetch-site", b"sec-fetch-mode", b"host"}
            | {b"authorization", self._header}
        )
        # Not HttpOnly: the page's JavaScript reads it to copy it into the header. Lax, not
        # Strict: it isn't a credential, and a Strict one would be missing (and replaced) on the
        # first request after every link from another site.
        self._cookie = (
            CookieSettings(
                cookie_name,
                secure=cookie_secure,
                same_site="lax",
                http_only=False,
                domain=cookie_domain,
            )
            if require_token
            else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cookie = self._cookie
        if cookie is not None:
            send = self._issue_token(cookie, scope, send)
        if scope["method"] in SAFE_METHODS or self._exempted(scope):
            await self.app(scope, receive, send)
            return
        found = self._headers(scope)
        credentials = _has_credentials(scope, found)
        refusal = self._refusal(scope, found, credentials, websocket=False)
        detail = "Cross-site request refused"
        if cookie is not None and credentials:
            token_ok = self._token_ok(cookie, scope, found)
            if refusal == _NO_SOURCE and token_ok:
                refusal = None
            elif refusal is None and not token_ok:
                refusal = ("token", "no valid CSRF token")
                detail = "Missing or invalid CSRF token"
        if refusal is not None:
            logger.info("Refused a cross-site %s request: %s", scope["method"], refusal[1])
            _record(scope, "http", refusal[0])
            await _respond(send, _FORBIDDEN, detail)
            return
        await self.app(scope, receive, send)

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._exempted(scope):
            found = self._headers(scope)
            refusal = self._refusal(scope, found, _has_credentials(scope, found), websocket=True)
            if refusal is not None:
                logger.info("Refused a cross-site WebSocket handshake: %s", refusal[1])
                _record(scope, "websocket", refusal[0])
                # Closing before accepting: the server answers the handshake with a 403.
                if (await receive())["type"] == "websocket.connect":
                    await send({"type": "websocket.close", "code": _POLICY_VIOLATION})
                return
        await self.app(scope, receive, send)

    def _exempted(self, scope: Scope) -> bool:
        path = _route_path(scope)
        return any(path == base or path.startswith(base + "/") for base in self._exempt)

    def _headers(self, scope: Scope) -> dict[bytes, list[bytes]]:
        found: dict[bytes, list[bytes]] = {}
        for name, value in scope["headers"]:
            if name in self._read:
                found.setdefault(name, []).append(value)
        return found

    def _refusal(
        self, scope: Scope, found: dict[bytes, list[bytes]], credentials: bool, websocket: bool
    ) -> tuple[str, str] | None:
        """Why the request must be refused (for the metrics, and in words); None when it may go
        through."""
        checked = credentials or self.check_all
        if websocket and not checked:
            return None  # nothing the browser added by itself: nothing to hijack
        raw = _one(found, b"origin")
        origin = None if raw is None else (_origin(raw, strict=True) or _INVALID)
        site = _one(found, b"sec-fetch-site")
        if site is not None:
            if site in ("same-origin", "none") or origin in self.trusted_origins:
                return None
            if not checked and _one(found, b"sec-fetch-mode") == "cors":
                return None  # a cross-origin API call, without the browser's credentials
            return "fetch_site", f"Sec-Fetch-Site {_shown(site)}"
        if not checked:
            return None
        if origin is None:
            if websocket:
                return None  # browsers always send Origin on handshakes: this isn't one
            referer = _one(found, b"referer")
            if referer is None:
                return _NO_SOURCE
            origin = _origin(referer, strict=False) or _INVALID
        if origin in self.trusted_origins or origin == _own_origin(scope, found):
            return None
        return "origin", f"origin {_shown(origin)}"

    def _token_ok(
        self, cookie: CookieSettings, scope: Scope, found: dict[bytes, list[bytes]]
    ) -> bool:
        tokens = cookie_values(scope, cookie.name)
        header = _one(found, self._header)
        if len(tokens) != 1 or header is None or not _TOKEN.fullmatch(tokens[0]):
            return False
        return hmac.compare_digest(tokens[0].encode(), header.encode("latin-1"))

    def _issue_token(self, cookie: CookieSettings, scope: Scope, send: Send) -> Send:
        """Expose the request's token in `request.state.csrf_token`, and set a new one on the
        response when the request has no valid one."""
        tokens = cookie_values(scope, cookie.name)
        state = scope.setdefault("state", {})
        if len(tokens) == 1 and _TOKEN.fullmatch(tokens[0]):
            state["csrf_token"] = tokens[0]
            return send
        token = secrets.token_urlsafe(32)
        state["csrf_token"] = token
        set_cookie = cookie.header(token)

        async def send_with_token(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), set_cookie]}
            await send(message)

        return send_with_token


def _record(scope: Scope, kind: str, reason: str) -> None:
    metrics_of(HTTPConnection(scope)).count("csrf.refused", kind=kind, reason=reason)
    add_event("fastapi_alcazaba.csrf.refused", kind=kind, reason=reason)


def _has_credentials(scope: Scope, found: dict[bytes, list[bytes]]) -> bool:
    """Whether the request carries something the browser adds by itself: cookies or remembered
    HTTP authentication. A bearer token was put there by the page, which only the app's own
    pages (or those CORS lets through) can do."""
    if has_cookies(scope):
        return True
    for value in found.get(b"authorization", []):
        scheme = value.split(None, 1)[0].lower() if value.strip() else b""
        if scheme in _AMBIENT_SCHEMES:
            return True
    return False


def _own_origin(scope: Scope, found: dict[bytes, list[bytes]]) -> str | None:
    """The app's origin as the server sees it: the request's scheme and `Host` header."""
    host = _one(found, b"host")
    if host is None:
        return None
    scheme = str(scope.get("scheme", "http"))
    return _origin(f"{_WEBSOCKET_SCHEMES.get(scheme, scheme)}://{host}", strict=True)
