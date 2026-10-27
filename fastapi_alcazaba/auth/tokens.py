"""Revoked tokens and one-time codes, kept in the cache's backend so that every process sees them.

A JWT can't be taken back once issued: logging out or changing the password only works if every
request checks a list of revoked tokens until they expire. One-time codes (sent by e-mail or SMS
to log in, confirm an address or reset a password) need a store shared by every process as well,
an expiry, a limit on wrong guesses and single use. Both keep SHA-256 digests or HMACs: token ids,
subjects and codes never reach the backend in clear.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import secrets
import time
from datetime import datetime

from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.core.integrity import Secrets, secret_list
from fastapi_alcazaba.response_cache.caches import Cache, resolve_cache

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: A moment: Unix seconds (a JWT's `exp` or `iat`) or a datetime with its time zone.
Moment = float | datetime


def _timestamp(moment: Moment) -> float:
    if isinstance(moment, datetime):
        if moment.utcoffset() is None:
            raise ValueError("naive datetimes are ambiguous: give it a time zone (e.g. UTC)")
        return moment.timestamp()
    return float(moment)


def _digest(*parts: str) -> str:
    """SHA-256 of the parts, JSON-encoded so that no two lists of parts give the same text."""
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _cut_off(issued: float | None, cutoff: bytes | None) -> bool:
    """Whether a token issued at `issued` is before the subject's `cutoff`."""
    if cutoff is None or issued is None:
        return False
    try:
        return issued < int(cutoff)
    except ValueError:
        return False  # not a cutoff we stored


class TokenBlocklist:
    """Revoked tokens (by their `jti`) and subjects logged out everywhere (`revoke_subject`).

        blocklist = TokenBlocklist()

        async def current_user(claims: Annotated[dict[str, Any], Depends(decode_jwt)]) -> str:
            sub, iat = claims["sub"], claims["iat"]
            if await blocklist.is_revoked(claims["jti"], subject=sub, issued_at=iat):
                raise HTTPException(401)
            return sub

    Each mark lasts while the token could still be accepted, `leeway` included, and then the
    backend drops it. When the cache has a `signing_key` or `encryption_key`, the marks are sealed
    with it and planted ones are ignored; but whoever can delete keys in the backend can still
    un-revoke tokens, so keep it out of reach.

    :param cache: the Cache whose backend keeps the marks; FastAPICache's by default, resolved at
        each call (so the blocklist can be created before `FastAPICache.init`).
    :param namespace: part of the keys, after the cache's prefix (see `Cache.state_key`).
    :param fail_open: when the backend fails, `is_revoked` answers False (tokens are accepted)
        instead of True (refused, the default).
    :param leeway: seconds your JWT library still accepts a token after its `exp` (PyJWT's
        `leeway`): marks are kept that much longer.
    """

    def __init__(
        self,
        cache: Cache | None = None,
        *,
        namespace: str = "revoked",
        fail_open: bool = False,
        leeway: int = 60,
    ) -> None:
        if leeway < 0:
            raise ValueError("leeway can't be negative")
        self.cache = cache
        self.namespace = namespace
        self.fail_open = fail_open
        self.leeway = leeway

    def _key(self, cache: Cache, kind: str, value: str) -> str:
        return cache.state_key(self.namespace, f"{kind}:{_digest(value)}")

    async def _store(self, cache: Cache, key: str, payload: bytes, expire: int) -> None:
        protector = cache.options.protector
        value = protector.seal(key, payload) if protector is not None else payload
        backend = shared_backend(cache.backend)
        await cache.guard.run("set", lambda: backend.set(key, value, expire))

    async def _load(self, cache: Cache, key: str) -> bytes | None:
        backend = shared_backend(cache.backend)
        data = await cache.guard.run("get", lambda: backend.get(key))
        protector = cache.options.protector
        if data is None or protector is None:
            return data
        return protector.open(key, data)

    async def revoke(
        self,
        token_id: str,
        *,
        expires_at: Moment | None = None,
        ttl: int | None = None,
    ) -> None:
        """Refuse the token `token_id` (its `jti`) until it would have expired anyway: give its
        `exp` as `expires_at`, or the seconds it has left as `ttl`. Raises BackendUnavailable
        when the backend fails: the token isn't revoked then."""
        if ttl is None:
            if expires_at is None:
                raise ValueError("pass expires_at or ttl: without them the mark would stay forever")
            left = _timestamp(expires_at) - time.time()
        elif expires_at is None:
            left = ttl
        else:
            raise ValueError("pass expires_at or ttl, not both")
        expire = math.ceil(left + self.leeway)
        if expire <= 0:
            return  # it can't be accepted anymore
        cache = resolve_cache(self.cache)
        await self._store(cache, self._key(cache, "token", token_id), b"1", expire)

    async def revoke_subject(self, subject: str, *, ttl: int) -> None:
        """Refuse every token of `subject` issued before the current second: log out everywhere,
        e.g. after a password change. `ttl` is the longest lifetime of your tokens. `iat` has
        whole seconds, so the tokens issued during this second stay valid, among them the one you
        issue right after for the current session. Raises BackendUnavailable when the backend
        fails."""
        if ttl < 1:
            raise ValueError("ttl must be at least 1 second")
        cache = resolve_cache(self.cache)
        cutoff = str(int(time.time())).encode()
        key = self._key(cache, "subject", subject)
        await self._store(cache, key, cutoff, ttl + self.leeway)

    async def is_revoked(
        self,
        token_id: str,
        *,
        subject: str | None = None,
        issued_at: Moment | None = None,
    ) -> bool:
        """Whether the token is refused: its `jti` was revoked, or its subject was logged out
        everywhere after it was issued (give its `sub` and `iat`). When the backend fails,
        `not fail_open`."""
        if (subject is None) != (issued_at is None):
            raise ValueError("pass subject and issued_at together")
        issued = None if issued_at is None else _timestamp(issued_at)
        cache = resolve_cache(self.cache)
        token_key = self._key(cache, "token", token_id)
        try:
            if subject is None:
                mark, cutoff = await self._load(cache, token_key), None
            else:
                mark, cutoff = await asyncio.gather(
                    self._load(cache, token_key),
                    self._load(cache, self._key(cache, "subject", subject)),
                )
        except BackendUnavailable as exc:
            verdict = "accepted" if self.fail_open else "refused"
            logger.warning("Couldn't check whether a token is revoked: %s", verdict, exc_info=exc)
            cache.metrics.count("tokens.checks", outcome="unchecked")
            return not self.fail_open
        revoked = mark is not None or _cut_off(issued, cutoff)
        cache.metrics.count("tokens.checks", outcome="revoked" if revoked else "valid")
        return revoked


class OneTimeCodes:
    """Codes of `digits` digits, sent by e-mail or SMS, that prove who someone is: each lasts
    `ttl` seconds, works once and is lost after `max_attempts` wrong guesses.

        codes = OneTimeCodes(secret=settings.otp_secret)

        code = await codes.issue(user.email, purpose="login")  # send it to the user
        ...
        if not await codes.verify(form.email, form.code, purpose="login"):
            raise HTTPException(400, "Invalid or expired code")

    The backend holds an HMAC of each code with `secret`, never the code: a plain digest of a
    6-digit code is brute-forced in milliseconds by whoever reads the backend. Issuing a code
    replaces the previous one and gives `max_attempts` again, so put a FastAPIRateLimiter on the
    route that issues them.

    :param secret: at least 32 bytes (`secrets.token_urlsafe(32)`), from the environment or a
        secret manager. A list rotates them: the first one is used, all of them are accepted.
    :param cache: the Cache whose backend keeps the codes; FastAPICache's by default, resolved at
        each call.
    :param digits: length of the codes, 4 to 10.
    :param ttl: seconds each code lasts.
    :param max_attempts: guesses allowed for each code, right or wrong.
    :param namespace: part of the keys, after the cache's prefix (see `Cache.state_key`).
    """

    def __init__(
        self,
        secret: Secrets,
        cache: Cache | None = None,
        *,
        digits: int = 6,
        ttl: int = 300,
        max_attempts: int = 5,
        namespace: str = "otp",
    ) -> None:
        if not 4 <= digits <= 10:
            raise ValueError("digits must be between 4 and 10")
        if ttl < 1:
            raise ValueError("ttl must be at least 1 second")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._secrets = secret_list(secret)
        self.cache = cache
        self.digits = digits
        self.ttl = ttl
        self.max_attempts = max_attempts
        self.namespace = namespace

    def _keys(self, cache: Cache, subject: str, purpose: str) -> tuple[str, str]:
        base = cache.state_key(self.namespace, _digest(purpose, subject))
        return f"{base}:code", f"{base}:attempts"

    @staticmethod
    def _mac(secret: bytes, subject: str, purpose: str, code: str) -> bytes:
        # Bound to the purpose and the subject: a code copied to another key in the backend fails.
        message = json.dumps([purpose, subject, code]).encode()
        return hmac.new(secret, message, hashlib.sha256).hexdigest().encode()

    async def issue(self, subject: str, purpose: str = "") -> str:
        """A new code for `subject` (an e-mail address, a user id...) and `purpose` ("login",
        "reset-password"...), replacing the previous one. Raises BackendUnavailable when the
        backend fails."""
        cache = resolve_cache(self.cache)
        code_key, attempts_key = self._keys(cache, subject, purpose)
        code = f"{secrets.randbelow(10**self.digits):0{self.digits}d}"
        mac = self._mac(self._secrets[0], subject, purpose, code)
        backend = shared_backend(cache.backend)
        await cache.guard.run("delete", lambda: backend.clear(key=attempts_key))
        await cache.guard.run("set", lambda: backend.set(code_key, mac, self.ttl))
        return code

    async def verify(self, subject: str, code: str, purpose: str = "") -> bool:
        """Whether `code` is the current code of `subject` for `purpose`; it's used up then. Of
        several requests with the right code at the same time, only one gets True. False when the
        backend fails."""
        cache = resolve_cache(self.cache)
        outcome = await self._verify(cache, subject, code, purpose)
        cache.metrics.count("otp.verifications", outcome=outcome)
        return outcome == "success"

    async def _verify(self, cache: Cache, subject: str, code: str, purpose: str) -> str:
        backend, guard = shared_backend(cache.backend), cache.guard
        code_key, attempts_key = self._keys(cache, subject, purpose)
        try:
            # Counted first: every guess counts, malformed or concurrent ones too.
            attempts = await guard.run("incr", lambda: backend.incr(attempts_key, 1, self.ttl))
            if attempts > self.max_attempts:
                await guard.run("delete", lambda: backend.clear(key=code_key))
                return "exhausted"
            if len(code) != self.digits or not (code.isascii() and code.isdigit()):
                return "malformed"
            stored = await guard.run("get", lambda: backend.get(code_key))
            if stored is None:
                return "missing"
            macs = [self._mac(secret, subject, purpose, code) for secret in self._secrets]
            if not any(hmac.compare_digest(stored, mac) for mac in macs):
                return "wrong"
            # Used up by whoever deletes it: of two requests with the right code, one wins.
            if await guard.run("delete", lambda: backend.clear(key=code_key)) != 1:
                return "missing"
        except BackendUnavailable as exc:
            logger.warning("Couldn't verify a one-time code", exc_info=exc)
            return "unchecked"
        try:
            await guard.run("delete", lambda: backend.clear(key=attempts_key))
        except BackendUnavailable as exc:
            logger.debug("Couldn't reset the attempts at a used code (they expire): %s", exc)
        return "success"
