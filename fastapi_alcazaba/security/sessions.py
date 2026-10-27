"""Server-side sessions: the data lives in the cache's backend, and the cookie only carries a
random id. `request.session` works as with Starlette's SessionMiddleware.

    app.add_middleware(ServerSessionMiddleware, max_age=8 * 3600, idle_timeout=1800)

    @app.post("/login")
    async def login(request: Request, form: Annotated[Login, Form()]) -> RedirectResponse:
        user = await authenticate(form)
        request.session["user_id"] = user.id
        rotate_session(request)  # an id planted in the browser before the login is now worthless
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        clear_session(request)  # deleted from the backend: the old cookie is worth nothing
        return RedirectResponse("/", status_code=303)

Starlette's SessionMiddleware keeps the whole session in a signed cookie: the client can read it
(it's only base64), it can't be revoked (a copied cookie keeps working after logout until it
expires) and it can't hold much more than 4 KB. Here the cookie holds 256 random bits; the data is
kept under a SHA-256 digest of them (whoever reads the backend's keys can't use them as cookies),
sealed with the cache's signing or encryption key, and logging out deletes it.
"""

import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.asgi import respond
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.response_cache.caches import Cache, get_cache
from fastapi_alcazaba.security._cookies import CookieSettings, SameSite, cookie_values

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_ID = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32): 256 bits
_UNAVAILABLE = 503


class ServerSession(dict[str, Any]):
    """The session of a request (`request.session`): a dict of JSON values, saved when the
    response starts if it changed (nested changes too).

    Once it's read the response depends on the cookie, and gets `Vary: Cookie`. Starlette 1.0+
    tells it on every `request.session`; older ones don't, so its reads tell it too."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        super().__init__(data or {})
        self.accessed = False
        self.rotate = False
        self.cleared = False

    def mark_accessed(self) -> None:
        """Called by `request.session`: the response then depends on the cookie."""
        self.accessed = True


def _reader(name: str) -> Any:
    method = getattr(dict, name)

    def read(self: ServerSession, *args: Any, **kwargs: Any) -> Any:
        self.accessed = True
        return method(self, *args, **kwargs)

    read.__name__ = read.__qualname__ = name
    return read


for _name in (
    "__contains__",
    "__getitem__",
    "__iter__",
    "__len__",
    "get",
    "items",
    "keys",
    "pop",
    "setdefault",
    "values",
):
    setattr(ServerSession, _name, _reader(_name))


@dataclass(slots=True)
class _Loaded:
    """What the request came with."""

    key: str | None  # backend key of the session the cookie points at, if it exists
    original: bytes  # its data as JSON, to tell whether it changed
    created: int  # Unix seconds
    seen: int
    cookie_sent: bool
    unavailable: bool = False  # the backend couldn't be read: nothing may be written


def _dump(data: dict[str, Any]) -> bytes:
    # Not sorted: keys come back in the order they were stored, as with Starlette's sessions.
    return json.dumps(data, separators=(",", ":"), allow_nan=False).encode()


def _count(cache: Cache, event: str) -> None:
    cache.metrics.count("sessions", event=event)


def _session(connection: HTTPConnection) -> ServerSession:
    session = connection.scope.get("session")
    if not isinstance(session, ServerSession):
        raise RuntimeError("ServerSessionMiddleware isn't installed")
    if connection.scope["type"] != "http":
        raise RuntimeError("sessions can only be changed in HTTP requests, not in WebSockets")
    return session


def rotate_session(connection: HTTPConnection) -> None:
    """Give the session a new id, with the same data, when the response starts. Call it when the
    user logs in or gains privileges: an id someone planted in their browser before (session
    fixation) is deleted with the old session. The new session's `max_age` starts then."""
    _session(connection).rotate = True


def clear_session(connection: HTTPConnection) -> None:
    """Log out: empty the session, delete it from the backend and expire the cookie when the
    response starts. Data stored afterwards in the same request goes to a new session."""
    session = _session(connection)
    session.clear()
    session.cleared = True


class ServerSessionMiddleware:
    """ASGI middleware that keeps each browser's session in the cache's backend, under a random
    id carried by a cookie (HttpOnly, Secure and SameSite by default).

        app.add_middleware(ServerSessionMiddleware)

    - **Saved only when it changed**, when the response starts; an empty session isn't stored
      and gets no cookie. A session emptied by the app is deleted and its cookie expired.
    - **New ids are always the server's:** a cookie whose session doesn't exist (expired, made
      up) never becomes the id of a new one. `rotate_session` at login, `clear_session` at logout.
    - **Expiry:** `max_age` seconds after the session was created (or rotated), and after
      `idle_timeout` seconds without requests. Both are checked when it's read, and the backend
      drops it then too.
    - **Backend failures:** if the session can't be read, the request goes on with an empty one
      (anonymous: fail closed) and a warning; a change to it, or any failure to save, rotate or
      clear, replaces the response with a 503 and leaves the cookie alone, since what the
      response says (logged in, logged out) wouldn't be true. Several requests of the same
      session at once: the last one to save wins.
    - **Sealed** with the cache's `signing_key` or `encryption_key` (use the latter when sessions
      hold personal data). Without either, whoever can write to the backend can forge sessions: a
      warning is logged.
    - **WebSockets** get `websocket.session` too, read-only.

    :param cache: where sessions are kept; the app's (`Cache.install`) or FastAPICache's by
        default. With a TieredBackend, only its shared (remote) tier is used.
    :param cookie_name: the cookie. `__Host-session` keeps sibling subdomains from setting one
        (cookie tossing); it needs `secure`, path `/` and no domain.
    :param max_age: seconds a session lasts from its creation; also the cookie's `Max-Age`.
    :param idle_timeout: seconds without requests after which it's over; None for no limit.
    :param secure: only send the cookie over HTTPS.
    :param same_site: the cookie's SameSite: "lax", "strict" or "none" (needs `secure`).
    :param path: the cookie's path.
    :param domain: the cookie's domain; None keeps it to the host that set it.
    :param max_size: bytes of JSON a session may take; more raises ValueError when it's saved.
    :param namespace: part of the backend keys, after the cache's prefix (`Cache.state_key`).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        cache: Cache | None = None,
        cookie_name: str = "session",
        max_age: int = 14 * 24 * 3600,
        idle_timeout: int | None = None,
        secure: bool = True,
        same_site: SameSite = "lax",
        path: str = "/",
        domain: str | None = None,
        max_size: int = 16_384,
        namespace: str = "session",
    ) -> None:
        if max_age < 1:
            raise ValueError("max_age must be at least 1 second")
        if idle_timeout is not None and not 1 <= idle_timeout <= max_age:
            raise ValueError("idle_timeout must be between 1 second and max_age")
        if max_size < 2:
            raise ValueError("max_size must be at least 2 bytes")
        self.app = app
        self.cache = cache
        self.cookie = CookieSettings(
            cookie_name, secure=secure, same_site=same_site, path=path, domain=domain
        )
        self.max_age = max_age
        self.idle_timeout = idle_timeout
        self.max_size = max_size
        self.namespace = namespace
        # Seen less often than this is rewritten, to slide the idle timeout without a write on
        # every request.
        self._touch_after = max(1, idle_timeout // 10) if idle_timeout is not None else None
        self._warned = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        cache = self.cache if self.cache is not None else get_cache(HTTPConnection(scope))
        if cache.options.protector is None and not self._warned:
            self._warned = True
            logger.warning(
                "Sessions are stored unsigned: whoever can write to the backend can forge one. "
                "Give the cache a signing_key or an encryption_key"
            )
        session, loaded = await self._load(cache, scope)
        scope["session"] = session
        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return
        replaced = False

        async def send_with_session(message: Message) -> None:
            nonlocal replaced
            if replaced:
                return  # the app's response, which the 503 replaced
            if message["type"] == "http.response.start":
                accessed = session.accessed  # by the app: saving it reads it too
                try:
                    cookie = await self._commit(cache, session, loaded)
                except BackendUnavailable as exc:
                    logger.warning("Couldn't save the session: answering 503", exc_info=exc)
                    _count(cache, "unavailable")
                    replaced = True
                    await respond(scope, send, _UNAVAILABLE, "Service Unavailable")
                    return
                headers = list(message.get("headers", []))
                if cookie is not None:
                    headers.append(cookie)
                    if not any(name.lower() == b"cache-control" for name, _ in headers):
                        # A shared cache must never hand one user's session id to others.
                        headers.append((b"cache-control", b"no-store"))
                if accessed and not any(
                    name.lower() == b"vary" and (b"cookie" in value.lower() or b"*" in value)
                    for name, value in headers
                ):
                    headers.append((b"vary", b"Cookie"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_session)

    def _key(self, cache: Cache, session_id: str) -> str:
        return cache.state_key(self.namespace, hashlib.sha256(session_id.encode()).hexdigest())

    async def _load(self, cache: Cache, scope: Scope) -> tuple[ServerSession, _Loaded]:
        now = int(time.time())
        values = cookie_values(scope, self.cookie.name)
        fresh = _Loaded(None, b"{}", now, now, cookie_sent=bool(values))
        session_id = next((value for value in values if _ID.fullmatch(value)), None)
        if session_id is None:
            return ServerSession(), fresh
        key = self._key(cache, session_id)
        backend = shared_backend(cache.backend)
        try:
            stored = await cache.guard.run("get", lambda: backend.get(key))
        except BackendUnavailable as exc:
            logger.warning("Couldn't read the session: going on without it", exc_info=exc)
            _count(cache, "unreadable")
            fresh.unavailable = True
            return ServerSession(), fresh
        record = None if stored is None else self._open(cache, key, stored, now)
        if record is None:
            return ServerSession(), fresh
        data, created, seen = record
        return ServerSession(data), _Loaded(key, _dump(data), created, seen, cookie_sent=True)

    def _open(
        self,
        cache: Cache,
        key: str,
        stored: bytes,
        now: int,
    ) -> tuple[dict[str, Any], int, int] | None:
        """(data, created, seen) of a live session; None if it expired or isn't ours."""
        protector = cache.options.protector
        payload = protector.open(key, stored) if protector is not None else stored
        try:
            record: Any = json.loads(payload) if payload is not None else None
            data, created, seen = record["data"], record["created"], record["seen"]
        except (ValueError, TypeError, KeyError):
            data = created = seen = None
        if not isinstance(data, dict) or type(created) is not int or type(seen) is not int:
            logger.warning("Ignoring a session that isn't ours")
            _count(cache, "rejected")
            return None
        if now - created >= self.max_age:
            return None
        if self.idle_timeout is not None and now - seen >= self.idle_timeout:
            return None
        return data, created, seen

    async def _commit(
        self,
        cache: Cache,
        session: ServerSession,
        loaded: _Loaded,
    ) -> tuple[bytes, bytes] | None:
        """Save, rotate or delete the session as it ended up; the `Set-Cookie` header if the
        cookie changes. Raises BackendUnavailable when the backend fails."""
        data = _dump(session)  # TypeError or ValueError for what isn't JSON: the app's error
        if len(data) > self.max_size:
            raise ValueError(
                f"the session takes {len(data)} bytes, more than max_size ({self.max_size})"
            )
        now = int(time.time())
        renew = session.rotate or session.cleared
        if data == loaded.original and not renew:
            if loaded.key is not None and self._touch_due(loaded, now):
                try:
                    await self._write(cache, loaded.key, data, loaded.created, now)
                except BackendUnavailable as exc:
                    logger.debug("Couldn't refresh the session's idle timeout: %s", exc)
                else:
                    _count(cache, "touched")
            return None
        if loaded.unavailable:
            raise BackendUnavailable("the session couldn't be read, so it can't be written")
        backend = shared_backend(cache.backend)
        old_key = loaded.key
        if old_key is not None and (renew or not session):
            # Deleted first: if what follows fails, the old id (maybe planted) is gone anyway.
            await cache.guard.run("delete", lambda: backend.clear(key=old_key))
            if session.cleared or not session:
                _count(cache, "deleted")
        if not session:
            return self.cookie.expired() if loaded.cookie_sent else None
        if old_key is not None and not renew:
            await self._write(cache, old_key, data, loaded.created, now)
            _count(cache, "updated")
            return None
        session_id = secrets.token_urlsafe(32)
        await self._write(cache, self._key(cache, session_id), data, now, now)
        _count(cache, "rotated" if old_key is not None and not session.cleared else "created")
        return self.cookie.header(session_id, self.max_age)

    def _touch_due(self, loaded: _Loaded, now: int) -> bool:
        return self._touch_after is not None and now - loaded.seen >= self._touch_after

    async def _write(self, cache: Cache, key: str, data: bytes, created: int, now: int) -> None:
        record = b'{"created":%d,"seen":%d,"data":%s}' % (created, now, data)
        ttl = self.max_age - (now - created)
        if self.idle_timeout is not None:
            ttl = min(ttl, self.idle_timeout)
        protector = cache.options.protector
        stored = protector.seal(key, record) if protector is not None else record
        backend = shared_backend(cache.backend)
        await cache.guard.run("set", lambda: backend.set(key, stored, max(1, ttl)))
