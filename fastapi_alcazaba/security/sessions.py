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

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.asgi import respond
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.response_cache.caches import Cache, FastAPICache, get_cache
from fastapi_alcazaba.security._cookies import CookieSettings, SameSite, cookie_values

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_ID = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32): 256 bits
_UNAVAILABLE = 503
# Where in the request's state the backend key of its session (as it came) is kept.
_CURRENT = "fastapi_alcazaba_session_key"


class ServerSession(dict[str, Any]):
    """The session of a request (`request.session`): a dict of JSON values, saved when the
    response starts if it changed (nested changes too).

    Once it's read the response depends on the cookie, and gets `Vary: Cookie`. Starlette 1.0+
    tells it on every `request.session`; older ones don't, so its reads tell it too."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        super().__init__(data or {})
        self.accessed = False
        self.rotate = False
        self.new_id: str | None = None  # the id it got when saved, if it got a new one
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
    started: int = 0  # Unix milliseconds of `created`: what end_all compares


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
      session at once: the last one to save wins, but never against a logout or a rotation: the
      old id leaves a mark for the rest of its life, and a request still running with it can't
      bring it back.
    - **Sealed** with the cache's `signing_key` or `encryption_key` (use the latter when sessions
      hold personal data). Without either, whoever can write to the backend can forge sessions: a
      warning is logged.
    - **WebSockets** get `websocket.session` too, read-only.

    :param cache: where sessions are kept; the app's (`Cache.install`) or FastAPICache's by
        default. With a TieredBackend, only its shared (remote) tier is used.
    :param cookie_name: the cookie. `__Host-` keeps sibling subdomains from setting one (cookie
        tossing); it needs `secure`, path `/` and no domain: name it otherwise for plain HTTP in
        development.
    :param max_age: seconds a session lasts from its creation; also the cookie's `Max-Age`.
    :param idle_timeout: seconds without requests after which it's over; None for no limit.
    :param secure: only send the cookie over HTTPS.
    :param same_site: the cookie's SameSite: "lax", "strict" or "none" (needs `secure`).
    :param path: the cookie's path.
    :param domain: the cookie's domain; None keeps it to the host that set it.
    :param max_size: bytes of JSON a session may take; more raises ValueError when it's saved.
    :param namespace: part of the backend keys, after the cache's prefix (`Cache.state_key`).
    :param users: keeps each user's sessions (see `UserSessions`), to list them and end them,
        all of them at once too: sessions whose data has its `user_key`.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        cache: Cache | None = None,
        cookie_name: str = "__Host-session",
        max_age: int = 14 * 24 * 3600,
        idle_timeout: int | None = None,
        secure: bool = True,
        same_site: SameSite = "lax",
        path: str = "/",
        domain: str | None = None,
        max_size: int = 16_384,
        namespace: str = "session",
        users: "UserSessions | None" = None,
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
        self.users = users
        if users is not None:
            users.bind(namespace, max_age)
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
                    cookie = await self._commit(cache, session, loaded, scope)
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
        data, created, seen, started = record
        if self.users is not None and await self.users.ended(cache, data, started):
            return ServerSession(), fresh  # its user's sessions were all ended after it started
        scope.setdefault("state", {})[_CURRENT] = key
        loaded = _Loaded(key, _dump(data), created, seen, cookie_sent=True, started=started)
        return ServerSession(data), loaded

    def _open(
        self,
        cache: Cache,
        key: str,
        stored: bytes,
        now: int,
    ) -> tuple[dict[str, Any], int, int, int] | None:
        """(data, created, seen, started) of a live session; None if it expired or isn't ours."""
        protector = cache.options.protector
        payload = protector.open(key, stored) if protector is not None else stored
        try:
            record: Any = json.loads(payload) if payload is not None else None
            data, created, seen = record["data"], record["created"], record["seen"]
            started = record.get("started", created * 1000)
        except (ValueError, TypeError, KeyError, AttributeError):
            data = created = seen = started = None
        if not isinstance(data, dict) or not all(type(t) is int for t in (created, seen, started)):
            logger.warning("Ignoring a session that isn't ours")
            _count(cache, "rejected")
            return None
        if now - created >= self.max_age:
            return None
        if self.idle_timeout is not None and now - seen >= self.idle_timeout:
            return None
        return data, created, seen, started

    async def _commit(
        self,
        cache: Cache,
        session: ServerSession,
        loaded: _Loaded,
        scope: Scope,
    ) -> tuple[bytes, bytes] | None:
        """Save, rotate or delete the session as it ended up; the `Set-Cookie` header if the
        cookie changes. Raises BackendUnavailable when the backend fails."""
        cookie = await self._save(cache, session, loaded)
        if self.users is not None:
            await self.users.follow(cache, loaded, session, self._key, scope)
        return cookie

    async def _save(
        self,
        cache: Cache,
        session: ServerSession,
        loaded: _Loaded,
    ) -> tuple[bytes, bytes] | None:
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
                    await self._rewrite(cache, loaded.key, data, loaded, now)
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
            # Marked and deleted first: if what follows fails, the old id (maybe planted) is gone
            # anyway, and the mark keeps requests still running with it from saving it again.
            gone = _gone(old_key)
            ttl = max(1, self.max_age - (now - loaded.created))
            await cache.guard.run("set", lambda: backend.set(gone, b"1", ttl))
            await cache.guard.run("delete", lambda: backend.clear(key=old_key))
            if session.cleared or not session:
                _count(cache, "deleted")
        if not session:
            return self.cookie.expired() if loaded.cookie_sent else None
        if old_key is not None and not renew:
            await self._rewrite(cache, old_key, data, loaded, now)
            _count(cache, "updated")
            return None
        session_id = secrets.token_urlsafe(32)
        started = int(time.time() * 1000)
        await self._write(cache, self._key(cache, session_id), data, now, now, started)
        _count(cache, "rotated" if old_key is not None and not session.cleared else "created")
        session.new_id = session_id
        return self.cookie.header(session_id, self.max_age)

    def _touch_due(self, loaded: _Loaded, now: int) -> bool:
        return self._touch_after is not None and now - loaded.seen >= self._touch_after

    async def _rewrite(
        self,
        cache: Cache,
        key: str,
        data: bytes,
        loaded: _Loaded,
        now: int,
    ) -> None:
        """`_write` of a session that was loaded, unless a logout or a rotation ended it
        meanwhile. Checked after writing: the logout marks before it deletes, so either it
        deletes this write, or this check sees its mark and deletes it."""
        await self._write(cache, key, data, loaded.created, now, loaded.started)
        backend = shared_backend(cache.backend)
        if await cache.guard.run("get", lambda: backend.get(_gone(key))) is not None:
            await cache.guard.run("delete", lambda: backend.clear(key=key))
            logger.debug("Not saving a session that ended meanwhile")

    async def _write(
        self,
        cache: Cache,
        key: str,
        data: bytes,
        created: int,
        now: int,
        started: int,
    ) -> None:
        record = b'{"created":%d,"started":%d,"seen":%d,"data":%s}' % (created, started, now, data)
        ttl = self.max_age - (now - created)
        if self.idle_timeout is not None:
            ttl = min(ttl, self.idle_timeout)
        protector = cache.options.protector
        stored = protector.seal(key, record) if protector is not None else record
        backend = shared_backend(cache.backend)
        await cache.guard.run("set", lambda: backend.set(key, stored, max(1, ttl)))


def _gone(key: str) -> str:
    """Where a logout or a rotation marks the session of `key` as ended."""
    return f"{key}:gone"


async def _end(cache: Cache, key: str, ttl: int) -> None:
    """End the session of `key` as a logout does: marked first, so that a request still running
    with it can't save it again, then deleted."""
    backend = shared_backend(cache.backend)
    await cache.guard.run("set", lambda: backend.set(_gone(key), b"1", max(1, ttl)))
    await cache.guard.run("delete", lambda: backend.clear(key=key))


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """One of a user's sessions, as `UserSessions.list` gives it, for a page of "your devices".

    :param ref: what names it to `UserSessions.end`: not its id, which only its cookie holds.
    :param created: when it started (the login, or its last rotation), in UTC.
    :param last_seen: when it was last saved or touched (`idle_timeout` touches it), in UTC.
    :param user_agent: the browser's `User-Agent` when it started, cut at 200 characters.
    :param client: the client's address when it started, as the server reports it.
    :param current: it's the session of the request that asked.
    """

    ref: str
    created: datetime
    last_seen: datetime
    user_agent: str | None
    client: str | None
    current: bool


class UserSessions:
    """Each user's server sessions, to list them ("your devices") and end them: one, or all of
    them ("log out everywhere", a new password). Give it to the middleware:

        user_sessions = UserSessions()
        app.add_middleware(ServerSessionMiddleware, users=user_sessions)

        await user_sessions.end_all(user.id, request=request, keep_current=True)

    A session is the user's whose data has `user_key` (what `SessionStrategy.login` sets): the
    middleware keeps an index of each user's sessions in the backend, sealed like them, when
    they start, rotate and end.

    - **Ending all of them** also refuses, from then on, every session of the user that started
      before: those the index didn't hold too (two logins at once on a backend without locks,
      an index the backend lost). It costs one more read for each request with a user.
    - **At most `max_sessions`** per user: a login past it ends the oldest session.
    - **The index** holds each session's backend key (a digest: not a cookie), and its
      `User-Agent` and address when it started (`record_client=False` for neither).

    :param user_key: the key of the session's data that holds its user's ID.
    :param max_sessions: sessions a user may have at once.
    :param record_client: keep the `User-Agent` and the address of each session.
    :param cache: where the sessions are; the request's (`get_cache`) by default.
    """

    def __init__(
        self,
        *,
        user_key: str = "user_id",
        max_sessions: int = 50,
        record_client: bool = True,
        cache: Cache | None = None,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions is 1 or more")
        self.user_key = user_key
        self.max_sessions = max_sessions
        self.record_client = record_client
        self.cache = cache
        self.namespace: str | None = None
        self.max_age = 0

    def bind(self, namespace: str, max_age: int) -> None:
        """Called by the ServerSessionMiddleware it's given to: where its sessions are."""
        if self.namespace is not None and (namespace, max_age) != (self.namespace, self.max_age):
            raise ValueError("a UserSessions follows the sessions of one ServerSessionMiddleware")
        self.namespace, self.max_age = namespace, max_age

    # What the middleware calls.

    async def ended(self, cache: Cache, data: dict[str, Any], started: int) -> bool:
        """Whether a session of `data`, started at `started` (Unix milliseconds), was ended by
        `end_all`."""
        user = data.get(self.user_key)
        if user is None:
            return False
        backend = shared_backend(cache.backend)
        key = self._key(cache, "ended", user)
        try:
            cutoff = await cache.guard.run("get", lambda: backend.get(key))
        except BackendUnavailable as exc:
            logger.warning("Couldn't check whether the session was ended: refused", exc_info=exc)
            return True
        return cutoff is not None and started < int(cutoff)

    async def follow(
        self,
        cache: Cache,
        loaded: _Loaded,
        session: ServerSession,
        key_of: Callable[[Cache, str], str],
        scope: Scope,
    ) -> None:
        """Keep the index as the request left the session: started, rotated, ended, or with
        another user. A failure is logged: `end_all` refuses what the index misses."""
        before = json.loads(loaded.original).get(self.user_key)
        after = session.get(self.user_key) if session else None
        new_key = key_of(cache, session.new_id) if session.new_id is not None else None
        current = new_key or (loaded.key if session else None)
        moved = before != after or new_key is not None
        try:
            if loaded.key is not None and before is not None and moved:
                old = _ref(loaded.key)
                await self._change(cache, before, lambda index: index.pop(old, None))
            if current is not None and after is not None and moved:
                new = {_ref(current): self._entry(current, scope)}
                await self._change(cache, after, lambda index: index.update(new))
        except BackendUnavailable as exc:
            logger.warning("Couldn't keep the index of the user's sessions", exc_info=exc)

    # What the app calls.

    async def list(
        self,
        user_id: Any,
        *,
        request: HTTPConnection | None = None,
    ) -> list[SessionInfo]:
        """The user's live sessions, the newest first; those that expired are dropped from the
        index. Raises BackendUnavailable when the backend fails."""
        cache = self._cache(request)
        current = request.scope.get("state", {}).get(_CURRENT) if request is not None else None
        backend = shared_backend(cache.backend)
        found: list[SessionInfo] = []
        gone: list[str] = []
        for ref, entry in (await self._index(cache, user_id)).items():
            key: str = entry["key"]
            stored = await cache.guard.run("get", partial(backend.get, key))
            record = self._record(cache, key, stored, user_id)
            if record is None:
                gone.append(ref)
                continue
            found.append(
                SessionInfo(
                    ref=ref,
                    created=datetime.fromtimestamp(record["created"], UTC),
                    last_seen=datetime.fromtimestamp(record["seen"], UTC),
                    user_agent=entry.get("agent"),
                    client=entry.get("client"),
                    current=entry["key"] == current,
                )
            )
        if gone:
            await self._change(cache, user_id, lambda index: [index.pop(r, None) for r in gone])
        return sorted(found, key=lambda info: info.created, reverse=True)

    async def end(self, user_id: Any, ref: str, *, request: HTTPConnection | None = None) -> bool:
        """End the session `ref` of the user (from `list`); False when it isn't one of theirs.
        Ending the request's own session logs it out once the response is sent."""
        cache = self._cache(request)
        entry = (await self._index(cache, user_id)).get(ref)
        if entry is None:
            return False
        await _end(cache, entry["key"], self.max_age)
        await self._change(cache, user_id, lambda index: index.pop(ref, None))
        return True

    async def end_all(
        self,
        user_id: Any,
        *,
        request: HTTPConnection | None = None,
        keep_current: bool = False,
    ) -> int:
        """End every session of the user (with `keep_current`, but the request's, which gets a
        new id); how many the index held. Sessions it didn't hold are refused too."""
        cache = self._cache(request)
        backend = shared_backend(cache.backend)
        cutoff = str(int(time.time() * 1000)).encode()  # milliseconds: before it, refused
        key = self._key(cache, "ended", user_id)
        await cache.guard.run("set", lambda: backend.set(key, cutoff, self.max_age))
        current = request.scope.get("state", {}).get(_CURRENT) if request is not None else None
        index = await self._index(cache, user_id)
        ended = 0
        for entry in index.values():
            if keep_current and entry["key"] == current:
                continue
            await _end(cache, entry["key"], self.max_age)
            ended += 1
        if keep_current and current is not None and request is not None:
            # Started before the cutoff, it would be refused too: a new one, not before it.
            rotate_session(request)
        kept = {ref: e for ref, e in index.items() if keep_current and e["key"] == current}

        def only_kept(found: dict[str, dict[str, Any]]) -> None:
            found.clear()
            found.update(kept)

        await self._change(cache, user_id, only_kept)
        return ended

    # The index.

    def _cache(self, request: HTTPConnection | None) -> Cache:
        if self.namespace is None:
            raise RuntimeError("give the UserSessions to ServerSessionMiddleware(users=...)")
        if self.cache is not None:
            return self.cache
        return get_cache(request) if request is not None else FastAPICache.instance()

    def _key(self, cache: Cache, kind: str, user_id: Any) -> str:
        digest = hashlib.sha256(json.dumps([kind, str(user_id)]).encode()).hexdigest()
        return cache.state_key(self.namespace or "session", f"{kind}:{digest}")

    def _entry(self, key: str, scope: Scope) -> dict[str, Any]:
        entry: dict[str, Any] = {"key": key, "since": int(time.time())}
        if self.record_client:
            agent = dict(scope.get("headers", [])).get(b"user-agent")
            client = scope.get("client")
            entry["agent"] = agent.decode("latin-1")[:200] if agent else None
            entry["client"] = client[0] if client else None
        return entry

    async def _index(self, cache: Cache, user_id: Any) -> dict[str, dict[str, Any]]:
        key = self._key(cache, "user", user_id)
        backend = shared_backend(cache.backend)
        stored = await cache.guard.run("get", lambda: backend.get(key))
        protector = cache.options.protector
        payload = protector.open(key, stored) if protector and stored is not None else stored
        try:
            index = json.loads(payload) if payload is not None else {}
        except ValueError:
            index = {}
        return index if isinstance(index, dict) else {}

    async def _change(
        self,
        cache: Cache,
        user_id: Any,
        change: Callable[[dict[str, dict[str, Any]]], Any],
    ) -> None:
        """`change` the user's index, holding its lock (where the backend has locks): two logins
        at once don't leave one out. Past `max_sessions`, the oldest sessions end."""
        backend = shared_backend(cache.backend)
        key = self._key(cache, "user", user_id)
        token = secrets.token_hex(8)
        locked = False
        if backend.supports_locks:
            for _ in range(50):  # up to about 2.5 seconds
                if await cache.guard.run(
                    "acquire_lock", lambda: backend.acquire_lock(f"{key}:lock", token, 5)
                ):
                    locked = True
                    break
                await asyncio.sleep(0.05)
            if not locked:
                logger.warning("Changing the index of a user's sessions without its lock")
        try:
            index = await self._index(cache, user_id)
            change(index)
            oldest = sorted(index, key=lambda ref: index[ref].get("since", 0))
            for ref in oldest[: max(0, len(index) - self.max_sessions)]:
                await _end(cache, index.pop(ref)["key"], self.max_age)
            payload = json.dumps(index, separators=(",", ":")).encode()
            protector = cache.options.protector
            stored = protector.seal(key, payload) if protector is not None else payload
            if index:
                await cache.guard.run("set", lambda: backend.set(key, stored, self.max_age))
            else:
                await cache.guard.run("delete", lambda: backend.clear(key=key))
        finally:
            if locked:
                await cache.guard.run(
                    "release_lock", lambda: backend.release_lock(f"{key}:lock", token)
                )

    def _record(
        self,
        cache: Cache,
        key: str,
        stored: bytes | None,
        user_id: Any,
    ) -> dict[str, Any] | None:
        """The session's record, while it's alive and still the user's."""
        if stored is None:
            return None
        protector = cache.options.protector
        payload = protector.open(key, stored) if protector is not None else stored
        try:
            record = json.loads(payload) if payload is not None else None
        except ValueError:
            return None
        if not isinstance(record, dict) or not isinstance(record.get("data"), dict):
            return None
        if str(record["data"].get(self.user_key)) != str(user_id):
            return None
        return record


def _ref(key: str) -> str:
    """What names a session to its user: from its backend key, which is a digest already."""
    return hashlib.sha256(key.encode()).hexdigest()[:32]
