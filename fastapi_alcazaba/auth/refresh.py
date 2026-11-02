"""Refresh tokens for the app's own JWTs: short access tokens, and a refresh token that gets a new
pair and is used up in doing so (rotation). A refresh token used twice was copied: the second use
ends the whole login (its family), access tokens included.

    jwt = auth.use(JWTStrategy(settings.jwt_secret, blocklist=TokenBlocklist()))
    refresh = RefreshTokens(jwt, secret=settings.refresh_secret)

    @app.post("/token")
    async def log_in(form: Login) -> TokenPair:
        user = check_password(form)
        return await refresh.issue(user.id)

    @app.post("/token/refresh")
    async def exchange(body: Refresh) -> TokenPair:
        try:
            return await refresh.exchange(body.refresh_token)
        except RefreshRefused:
            raise HTTPException(401) from None

A refresh token is `r1.<family>.<generation>.<mac>`: the MAC, made with `secret`, binds the
login (the family, also the access tokens' `sid`), its user and the generation. The backend keeps
one record per family, never a token nor anything that makes one; any generation can be checked,
so a copy of an old token is told apart from a forged one.
"""

import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from starlette.requests import HTTPConnection

from fastapi_alcazaba.auth.strategies.base import AuthenticationFailed
from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.core.integrity import Secrets, secret_list
from fastapi_alcazaba.response_cache.caches import Cache, resolve_cache

if TYPE_CHECKING:
    from fastapi_alcazaba.audit import FastAPIAudit
    from fastapi_alcazaba.auth.strategies.jwt import JWTStrategy

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_TOKEN = re.compile(r"r1\.([A-Za-z0-9_-]{22})\.(0|[1-9][0-9]{0,9})\.([A-Za-z0-9_-]{43})")
#: The longest refresh token read; longer ones are malformed without a look.
MAX_TOKEN = 200
# Claims the strategy sets itself, or that would change whose token it is.
_RESERVED = frozenset({"sub", "sid", "iat", "exp", "nbf", "jti", "iss", "aud"})
# Seconds an exchange holds its family's lock, and waits for it.
_LOCK_TTL = 10.0
_LOCK_WAIT = 5.0


class RefreshRefused(AuthenticationFailed):
    """A refresh token that gets nothing: answer 401. `reason` says why (`malformed`,
    `unknown`, `forged`, `expired`, `revoked`, `reused`, `wrong_client`); never send it to the
    client, which would learn which of its tokens are alive."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"refresh token refused: {reason}")
        self.reason = reason


class TokenPair(BaseModel):
    """What a login or an exchange gives: return it from the endpoint as is (RFC 6749's names)."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105  # the type of token, not one
    #: Seconds the access token lasts.
    expires_in: int
    #: Seconds the login (the family) lasts at most.
    refresh_expires_in: int
    #: The login's id: the access tokens' `sid`, for `revoke_session`.
    session: str


@dataclass(slots=True)
class _Family:
    sub: str
    client: str | None
    salt: str
    claims: dict[str, Any]
    created: int
    generation: int
    rotated: float
    used: float


def parse_token(token: str) -> tuple[str, int, str] | None:
    """(family, generation, MAC) of a refresh token; None when it isn't one."""
    if len(token) > MAX_TOKEN:
        return None
    match = _TOKEN.fullmatch(token)
    if match is None:
        return None
    return match[1], int(match[2]), match[3]


def format_token(family: str, generation: int, mac: str) -> str:
    return f"r1.{family}.{generation}.{mac}"


def _mac(secret: bytes, family: str, record: _Family, generation: int) -> str:
    message = json.dumps([family, record.sub, record.salt, generation]).encode()
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _record(data: bytes) -> _Family:
    """The family stored as `data`; ValueError when it isn't one."""
    try:
        fields = json.loads(data)
        family = _Family(**fields)
        base64.b64decode(family.salt, validate=True)
    except (TypeError, binascii.Error, RecursionError) as exc:
        raise ValueError("not a family of refresh tokens") from exc
    numbers = (family.created, family.generation, family.rotated, family.used)
    if not (
        isinstance(family.sub, str)
        and (family.client is None or isinstance(family.client, str))
        and isinstance(family.claims, dict)
        and all(isinstance(n, int | float) and not isinstance(n, bool) for n in numbers)
        and isinstance(family.created, int)
        and isinstance(family.generation, int)
        and family.generation >= 0
    ):
        raise ValueError("not a family of refresh tokens")
    return family


class RefreshTokens:
    """Refresh tokens for the access tokens of `strategy`, each family (one login) kept in the
    cache's backend.

    - **Rotation:** `exchange` gives a new pair and uses the refresh token up, under the family's
      lock: two exchanges of one token run one after the other.
    - **Reuse:** a refresh token already exchanged, presented again after `grace` seconds, was
      copied: its family is revoked (refresh and access tokens), and it's logged, counted and
      audited. A token whose MAC is wrong is refused without revoking anything, so knowing a
      family's id (an access token's `sid`) doesn't let anyone end the login.
    - **Grace:** within `grace` seconds of an exchange, the token just exchanged gets the newest
      refresh token again (and a new access token): concurrent requests of one client end with
      one token.
    - **Lifetimes:** a family lasts `lifetime` seconds from the login, and `idle` without an
      exchange.
    - **Clients:** a family issued for a `client` only exchanges for that client; another one
      presenting its token revokes it.
    - **Revocation:** `revoke` (logout with the refresh token), `revoke_session` (by `sid`),
      `revoke_subject` (everywhere); the strategy's `TokenBlocklist` refuses the access tokens.

    :param strategy: the JWTStrategy that signs the access tokens: it must be able to sign, and
        have a `blocklist`, which revoked families' access tokens are put in.
    :param secret: at least 32 bytes (`secrets.token_urlsafe(32)`), from your settings, for the
        MACs of the refresh tokens. A list rotates it: the first one makes tokens, all of them
        check; a token made with an older one is exchanged for one made with the first.
    :param cache: the Cache whose backend keeps the families; FastAPICache's by default, resolved
        at each call. Its backend must hold locks.
    :param access_ttl: seconds each access token lasts.
    :param lifetime: seconds a family lasts from the login, however often it's used.
    :param idle: seconds a family lasts without an exchange.
    :param grace: seconds the refresh token just exchanged still gets the newest one, 0 to 60.
    :param namespace: part of the keys, after the cache's prefix (see `Cache.state_key`).
    :param audit: the audit trail that records each login's tokens issued
        (`auth.refresh.issue`), each exchange (`auth.refresh.exchange`, with the reason of a
        refusal) and each revocation (`auth.refresh.revoke`).
    """

    def __init__(
        self,
        strategy: "JWTStrategy",
        *,
        secret: Secrets,
        cache: Cache | None = None,
        access_ttl: int = 900,
        lifetime: int = 30 * 86_400,
        idle: int = 7 * 86_400,
        grace: float = 10,
        namespace: str = "refresh",
        audit: "FastAPIAudit | None" = None,
    ) -> None:
        if not strategy.can_issue:
            raise ValueError("the strategy can't sign tokens: give it a signing_key")
        if strategy.blocklist is None:
            raise ValueError(
                "give the JWTStrategy a blocklist (TokenBlocklist()): revoking a login must "
                "refuse its access tokens too"
            )
        if access_ttl < 1:
            raise ValueError("access_ttl must be at least 1 second")
        if not 1 <= idle <= lifetime:
            raise ValueError("idle must be at least 1 second, and at most lifetime")
        if not 0 <= grace <= 60:
            raise ValueError("grace is 0 to 60 seconds")
        self.strategy = strategy
        self.blocklist = strategy.blocklist
        self._secrets = secret_list(secret)
        self.cache = None if cache is None else _check_locks(cache)
        self.access_ttl = access_ttl
        self.lifetime = lifetime
        self.idle = idle
        self.grace = grace
        self.namespace = namespace
        self.audit = audit

    # region Issuing and exchanging

    async def issue(
        self,
        subject: str,
        *,
        client: str | None = None,
        request: HTTPConnection | None = None,
        **claims: Any,
    ) -> TokenPair:
        """A new login of `subject`: an access token with `claims` (and a `sid`, the family) and a
        refresh token, bound to `client` when given. Raises BackendUnavailable when the backend
        fails: nothing is issued then."""
        if reserved := sorted(_RESERVED & claims.keys()):
            raise ValueError(f"the strategy sets {', '.join(reserved)} itself")
        json.dumps(claims)  # TypeError now rather than at each exchange
        cache = _check_locks(resolve_cache(self.cache))
        now = time.time()
        family = secrets.token_urlsafe(16)
        record = _Family(
            sub=subject,
            client=client,
            salt=base64.b64encode(secrets.token_bytes(32)).decode(),
            claims=claims,
            created=int(now),
            generation=0,
            rotated=now,
            used=now,
        )
        await self._store(cache, family, record, now)
        cache.metrics.count("refresh", outcome="issued")
        await self._audit("auth.refresh.issue", "success", subject, request)
        return self._pair(family, record, now)

    async def exchange(
        self,
        refresh_token: str,
        *,
        client: str | None = None,
        request: HTTPConnection | None = None,
    ) -> TokenPair:
        """A new pair for `refresh_token`, which is used up; raises RefreshRefused when it gets
        none (answer 401), and BackendUnavailable when the backend fails (nothing is issued)."""
        cache = _check_locks(resolve_cache(self.cache))
        try:
            outcome, record, pair = await self._exchange(cache, refresh_token, client)
        except BackendUnavailable:
            cache.metrics.count("refresh", outcome="unchecked")
            raise
        cache.metrics.count("refresh", outcome=outcome)
        subject = None if record is None else record.sub
        await self._audit("auth.refresh.exchange", outcome, subject, request)
        if pair is None:
            raise RefreshRefused(outcome)
        return pair

    async def _exchange(
        self,
        cache: Cache,
        token: str,
        client: str | None,
    ) -> tuple[str, _Family | None, TokenPair | None]:
        parsed = parse_token(token)
        if parsed is None:
            return "malformed", None, None
        family, generation, mac = parsed
        async with self._locked(cache, family):
            record = await self._load(cache, family)
            if record is None:
                return "unknown", None, None
            if not self._genuine(family, record, generation, mac):
                return "forged", record, None
            now = time.time()
            if now >= record.created + self.lifetime or now >= record.used + self.idle:
                await self._delete(cache, family)
                return "expired", record, None
            if await self._cut_off(cache, record):
                await self._delete(cache, family)
                return "revoked", record, None
            if record.client is not None and client != record.client:
                await self._revoke(cache, family)
                logger.warning("A refresh token was presented by another client: login revoked")
                return "wrong_client", record, None
            if generation == record.generation:
                record.generation += 1
                record.rotated = now
                outcome = "rotated"
            elif generation == record.generation - 1 and now - record.rotated <= self.grace:
                outcome = "grace"
            else:
                await self._revoke(cache, family)
                logger.warning("A refresh token was used again: its login is revoked")
                return "reused", record, None
            record.used = now
            await self._store(cache, family, record, now)
            return outcome, record, self._pair(family, record, now)

    @contextlib.asynccontextmanager
    async def _locked(self, cache: Cache, family: str) -> AsyncIterator[None]:
        """The family's lock: one exchange or revocation of a login at a time."""
        name = f"{self.namespace}:{family}"
        lock = cache.lock(name, ttl=_LOCK_TTL, wait=_LOCK_WAIT, renew=False)
        if not await lock.acquire():
            raise BackendUnavailable("another exchange of the login held its lock too long")
        try:
            yield
        finally:
            await lock.release()

    def _genuine(self, family: str, record: _Family, generation: int, mac: str) -> bool:
        macs = [_mac(secret, family, record, generation) for secret in self._secrets]
        return any(hmac.compare_digest(mac, expected) for expected in macs)

    def _pair(self, family: str, record: _Family, now: float) -> TokenPair:
        access = self.strategy.issue(
            record.sub, expires_in=self.access_ttl, sid=family, **record.claims
        )
        mac = _mac(self._secrets[0], family, record, record.generation)
        return TokenPair(
            access_token=access,
            refresh_token=format_token(family, record.generation, mac),
            expires_in=self.access_ttl,
            refresh_expires_in=max(0, math.floor(record.created + self.lifetime - now)),
            session=family,
        )

    # endregion

    # region Revoking

    async def revoke(
        self,
        refresh_token: str,
        *,
        request: HTTPConnection | None = None,
    ) -> bool:
        """End the login of `refresh_token` (log out on this device): its refresh tokens and,
        through the blocklist, its access tokens are refused. False when it isn't a genuine token
        of a live login. Raises BackendUnavailable when the backend fails."""
        parsed = parse_token(refresh_token)
        if parsed is None:
            return False
        family, generation, mac = parsed
        cache = _check_locks(resolve_cache(self.cache))
        async with self._locked(cache, family):
            record = await self._load(cache, family)
            if record is None or not self._genuine(family, record, generation, mac):
                return False
            await self._revoke(cache, family)
        await self._audit("auth.refresh.revoke", "success", record.sub, request)
        return True

    async def revoke_session(
        self,
        session: str,
        *,
        request: HTTPConnection | None = None,
    ) -> None:
        """End the login whose id is `session` (the `sid` of its access tokens, `TokenPair`'s
        `session`): for an operator, or a list of the user's devices. Raises BackendUnavailable
        when the backend fails."""
        cache = _check_locks(resolve_cache(self.cache))
        async with self._locked(cache, session):
            record = await self._load(cache, session)
            await self._revoke(cache, session)
        subject = None if record is None else record.sub
        await self._audit("auth.refresh.revoke", "success", subject, request)

    async def revoke_subject(
        self,
        subject: str,
        *,
        request: HTTPConnection | None = None,
    ) -> None:
        """End every login of `subject` started before the current second (log out everywhere,
        e.g. after a password change): the logins started during this second stay, among them
        the one you start right after. Raises BackendUnavailable when the backend fails."""
        cache = resolve_cache(self.cache)
        key = self._key(cache, "subject", subject)
        await self._put(cache, key, str(int(time.time())).encode(), self.lifetime)
        await self.blocklist.revoke_subject(subject, ttl=self.access_ttl)
        await self._audit("auth.refresh.revoke", "success", subject, request)

    async def _revoke(self, cache: Cache, family: str) -> None:
        await self._delete(cache, family)
        await self.blocklist.revoke_session(family, ttl=self.access_ttl)

    async def _cut_off(self, cache: Cache, record: _Family) -> bool:
        cutoff = await self._get(cache, self._key(cache, "subject", record.sub))
        try:
            return cutoff is not None and record.created < int(cutoff)
        except ValueError:
            return False  # not a cutoff we stored

    # endregion

    # region Storage

    def _key(self, cache: Cache, kind: str, value: str) -> str:
        return cache.state_key(self.namespace, f"{kind}:{_digest(value)}")

    async def _load(self, cache: Cache, family: str) -> _Family | None:
        data = await self._get(cache, self._key(cache, "family", family))
        if data is None:
            return None
        try:
            return _record(data)
        except ValueError:
            logger.warning("Ignoring a family of refresh tokens that can't be read")
            return None

    async def _store(self, cache: Cache, family: str, record: _Family, now: float) -> None:
        ends = min(record.created + self.lifetime, record.used + self.idle)
        expire = max(1, math.ceil(ends - now))
        payload = json.dumps(asdict(record), separators=(",", ":")).encode()
        await self._put(cache, self._key(cache, "family", family), payload, expire)

    async def _delete(self, cache: Cache, family: str) -> None:
        key = self._key(cache, "family", family)
        backend = shared_backend(cache.backend)
        await cache.guard.run("delete", lambda: backend.clear(key=key))

    async def _put(self, cache: Cache, key: str, payload: bytes, expire: int) -> None:
        protector = cache.options.protector
        value = protector.seal(key, payload) if protector is not None else payload
        backend = shared_backend(cache.backend)
        await cache.guard.run("set", lambda: backend.set(key, value, expire))

    async def _get(self, cache: Cache, key: str) -> bytes | None:
        backend = shared_backend(cache.backend)
        data = await cache.guard.run("get", lambda: backend.get(key))
        protector = cache.options.protector
        if data is None or protector is None:
            return data
        return protector.open(key, data)

    # endregion

    async def _audit(
        self,
        action: str,
        outcome: str,
        subject: str | None,
        request: HTTPConnection | None,
    ) -> None:
        if self.audit is None:
            return
        success = outcome in ("success", "rotated", "grace")
        await self.audit.log(
            action,
            outcome="success" if success else "failure",
            actor=subject,
            reason=None if success else outcome,
            request=request,
        )


def _check_locks(cache: Cache) -> Cache:
    """An exchange holds its family's lock: without them two exchanges could both rotate."""
    backend = shared_backend(cache.backend)
    if not backend.supports_locks:
        raise NotImplementedError(
            f"{type(backend).__name__} can't hold locks, which RefreshTokens needs to use each "
            "refresh token once (every backend shipped with fastapi-alcazaba can)"
        )
    return cache
