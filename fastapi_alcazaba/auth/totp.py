"""A second factor: the codes of an authenticator app (RFC 6238's TOTP: Google Authenticator,
1Password, Authy...), recovery codes for a lost phone, and step-up for sensitive actions. Needs
the `crypto` extra (cryptography), which seals the secrets.

    totp = FastAPITOTP(issuer="Example", secret=settings.totp_secret)

    enrollment = totp.enroll(user.id, label=user.email)  # show enrollment.uri as a QR code
    ...                                                  # the user types the code it shows
    if await totp.verify(user.id, enrollment.sealed, form.code, request=request):
        await db.users.save_totp(user.id, enrollment.sealed)  # sealed: nothing in clear

    recovery = totp.recovery_codes(user.id)  # show recovery.codes once, store recovery.digests

At login, after the password: `await totp.verify(user.id, user.totp_sealed, form.code)`, then
`totp.mark(request, user.id)`; a route that needs a recent second factor takes
`Depends(totp.require_recent(current_user_id, max_age=300))`.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote, urlencode

from pydantic import SecretStr
from starlette.requests import HTTPConnection

from fastapi_alcazaba.auth import stepup
from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.encryption import Encrypter
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.core.integrity import Secrets, secret_list
from fastapi_alcazaba.response_cache.caches import Cache, resolve_cache

if TYPE_CHECKING:
    from fastapi_alcazaba.audit import FastAPIAudit

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: The hash of the codes: authenticator apps support SHA1, some SHA256 and SHA512 too.
Algorithm = Literal["SHA1", "SHA256", "SHA512"]
_HASHES = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}
#: Bytes of each secret: RFC 4226 recommends 160 bits.
SECRET_SIZE = 20
#: The letters of recovery codes: base32's, without those mistaken for others (0/O, 1/I/L).
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_RECOVERY_LENGTH = 10  # about 50 bits: out of reach of guesses held to max_attempts
# What the sealed secrets are bound to: their user, under this name.
_SEALED_PREFIX = "totp1."


@dataclass(frozen=True, slots=True)
class TOTPEnrollment:
    """A new secret for a user's authenticator app.

    :param uri: the `otpauth://` URI, to show as a QR code for the app to scan.
    :param secret: the secret in base32, for those who type it in the app.
    :param sealed: the secret encrypted for its user, to store with them once they've typed a
        code that `verify` accepts: it's worth nothing without `FastAPITOTP`'s secret.
    """

    uri: str
    secret: SecretStr
    sealed: str


@dataclass(frozen=True, slots=True)
class RecoveryCodes:
    """Codes that stand in for the app once each, for a lost phone.

    :param codes: to show the user once, to print or keep in a password manager.
    :param digests: to store with the user; `use_recovery_code` tells which one was used.
    """

    codes: list[str]
    digests: list[str]


def _counter_code(key: bytes, counter: int, digits: int, algorithm: Algorithm) -> str:
    """RFC 4226's HOTP: the code of `counter`."""
    digest = hmac.new(key, struct.pack(">Q", counter), _HASHES[algorithm]).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 10**digits:0{digits}d}"


def _digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _check_locks(cache: Cache) -> Cache:
    """A code is marked used with a lock: without them every code would fail, unexplained."""
    backend = shared_backend(cache.backend)
    if not backend.supports_locks:
        raise NotImplementedError(
            f"{type(backend).__name__} can't hold locks, which FastAPITOTP needs to accept each "
            "code once (every backend shipped with fastapi-alcazaba can)"
        )
    return cache


def _normalized(code: str) -> str:
    """A recovery code as it was made: upper case, without spaces nor dashes."""
    return code.replace("-", "").replace(" ", "").upper()


class FastAPITOTP:
    """The codes of authenticator apps as a second factor (RFC 6238), recovery codes, and
    step-up for the routes that need a recent one.

    - **The secrets** are sealed with AES-256-GCM, each for its user: store `sealed`, never the
      secret. A sealed secret copied to another user doesn't open, nor without `secret`.
    - **A code** is accepted within `window` steps of `period` seconds before or after now, for
      clocks out of step; each one once (RFC 6238, 5.2): another request with the same code, from
      someone who looked over the user's shoulder, is refused.
    - **Guesses:** after `max_attempts` wrong codes (recovery codes included), the user's codes
      are refused for `lockout` seconds, whatever they are: a 6-digit code isn't guessed.
    - **When the backend fails,** nothing is accepted: no code is checked for replay, nor guesses
      counted.

    :param issuer: the name the authenticator app shows for the account ("Example").
    :param secret: at least 32 bytes (`secrets.token_urlsafe(32)`), from your settings: it seals
        the TOTP secrets and makes the recovery codes' digests. A list rotates it: the first one
        seals, all of them open.
    :param cache: the Cache whose backend keeps the codes used and the guesses; FastAPICache's
        by default, resolved at each call. Its backend must hold locks (not memcached).
    :param digits: of each code, 6 or 8.
    :param period: seconds each code lasts.
    :param window: steps accepted before and after the current one.
    :param algorithm: SHA1, which every app supports, SHA256 or SHA512.
    :param max_attempts: wrong codes before the lockout.
    :param lockout: seconds the codes are refused after `max_attempts` wrong ones, counted from
        the first of them.
    :param namespace: part of the keys, after the cache's prefix (see `Cache.state_key`).
    :param audit: the audit trail that records each second factor checked (`auth.mfa`) and each
        recovery code tried (`auth.recovery_code`), with their outcome.
    """

    def __init__(
        self,
        *,
        issuer: str,
        secret: Secrets,
        cache: Cache | None = None,
        digits: int = 6,
        period: int = 30,
        window: int = 1,
        algorithm: Algorithm = "SHA1",
        max_attempts: int = 5,
        lockout: int = 900,
        namespace: str = "totp",
        audit: "FastAPIAudit | None" = None,
    ) -> None:
        if not issuer or ":" in issuer:
            raise ValueError("issuer is a name, without ':'")
        if digits not in (6, 8):
            raise ValueError("digits is 6 or 8")
        if not 15 <= period <= 300:
            raise ValueError("period is 15 to 300 seconds")
        if not 0 <= window <= 3:
            raise ValueError("window is 0 to 3 steps")
        if algorithm not in _HASHES:
            raise ValueError("algorithm is SHA1, SHA256 or SHA512")
        if max_attempts < 1 or lockout < 1:
            raise ValueError("max_attempts and lockout are 1 or more")
        self.issuer = issuer
        self._secrets = secret_list(secret)
        self._encrypter = Encrypter(self._secrets)
        self.cache = None if cache is None else _check_locks(cache)
        self.digits = digits
        self.period = period
        self.window = window
        self.algorithm = algorithm
        self.max_attempts = max_attempts
        self.lockout = lockout
        self.namespace = namespace
        self.audit = audit

    # Enrollment and the codes of the app.

    def enroll(self, user_id: str | int | uuid.UUID, label: str) -> TOTPEnrollment:
        """A new secret for `user_id`'s app; `label` is what the app shows under the issuer (their
        e-mail address, their username). Store `sealed` once a code of it has been verified."""
        if not label or ":" in label:
            raise ValueError("label is a name, without ':'")
        raw = secrets.token_bytes(SECRET_SIZE)
        encoded = base64.b32encode(raw).decode().rstrip("=")
        query = {
            "secret": encoded,
            "issuer": self.issuer,
            "algorithm": self.algorithm,
            "digits": self.digits,
            "period": self.period,
        }
        uri = f"otpauth://totp/{quote(self.issuer)}:{quote(label)}?{urlencode(query)}"
        sealed = self._encrypter.seal(self._bound(user_id), raw)
        return TOTPEnrollment(
            uri=uri,
            secret=SecretStr(encoded),
            sealed=_SEALED_PREFIX + base64.urlsafe_b64encode(sealed).decode(),
        )

    def code(self, user_id: str | int | uuid.UUID, sealed: str, at: float | None = None) -> str:
        """The code the app shows at `at` (now by default): for tests, not for checking codes."""
        counter = int((time.time() if at is None else at) // self.period)
        return _counter_code(self._open(user_id, sealed), counter, self.digits, self.algorithm)

    async def verify(
        self,
        user_id: str | int | uuid.UUID,
        sealed: str,
        code: str,
        *,
        request: HTTPConnection | None = None,
    ) -> bool:
        """Whether `code` is one the app of `user_id`, whose secret is `sealed`, shows now (or
        `window` steps away), and hasn't been used. False when the user is locked out by wrong
        codes, or the backend fails. Raises ValueError when `sealed` isn't `user_id`'s."""
        key = self._open(user_id, sealed)
        cache = _check_locks(resolve_cache(self.cache))
        outcome = await self._verify(cache, str(user_id), key, code)
        cache.metrics.count("totp.verifications", outcome=outcome)
        await self._audit("auth.mfa", outcome, user_id, request)
        return outcome == "success"

    async def _verify(self, cache: Cache, user_id: str, key: bytes, code: str) -> str:
        backend, guard = shared_backend(cache.backend), cache.guard
        try:
            if await self._locked_out(cache, user_id):
                return "locked"
            code = code.replace(" ", "")
            if len(code) != self.digits or not (code.isascii() and code.isdigit()):
                return "malformed"
            now = int(time.time() // self.period)
            for counter in range(now - self.window, now + self.window + 1):
                if hmac.compare_digest(
                    _counter_code(key, counter, self.digits, self.algorithm), code
                ):
                    break
            else:
                return "wrong"
            # Used once: of two requests with the same code, the one that marks it wins.
            used = self._key(cache, "used", user_id, str(counter))
            ttl = (2 * self.window + 2) * self.period
            token = secrets.token_hex(8)
            if not await guard.run("acquire_lock", lambda: backend.acquire_lock(used, token, ttl)):
                return "replayed"
            await self._forgive(cache, user_id)
        except BackendUnavailable as exc:
            logger.warning("Couldn't verify a TOTP code", exc_info=exc)
            return "unchecked"
        return "success"

    # Recovery codes.

    def recovery_codes(self, user_id: str | int | uuid.UUID, count: int = 10) -> RecoveryCodes:
        """`count` new recovery codes for `user_id`, which replace the old ones: show `codes`
        once, store `digests` (instead of the old ones)."""
        if not 1 <= count <= 50:
            raise ValueError("count is 1 to 50")
        codes = []
        for _ in range(count):
            letters = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LENGTH))
            codes.append(f"{letters[:5]}-{letters[5:]}")
        digests = [self._recovery_digest(self._secrets[0], user_id, code) for code in codes]
        return RecoveryCodes(codes=codes, digests=digests)

    async def use_recovery_code(
        self,
        user_id: str | int | uuid.UUID,
        code: str,
        digests: Collection[str],
        *,
        request: HTTPConnection | None = None,
    ) -> str | None:
        """The digest of `digests` that `code` is, used up: delete it from those stored. None
        when it's none of them, was used already, the user is locked out, or the backend fails.
        Counts as a guess like a wrong TOTP code."""
        cache = _check_locks(resolve_cache(self.cache))
        outcome, used = await self._use_recovery(cache, str(user_id), code, digests)
        cache.metrics.count("totp.recovery", outcome=outcome)
        await self._audit("auth.recovery_code", outcome, user_id, request)
        return used

    async def _use_recovery(
        self,
        cache: Cache,
        user_id: str,
        code: str,
        digests: Collection[str],
    ) -> tuple[str, str | None]:
        backend, guard = shared_backend(cache.backend), cache.guard
        try:
            if await self._locked_out(cache, user_id):
                return "locked", None
            candidates = {self._recovery_digest(secret, user_id, code) for secret in self._secrets}
            matching = (
                stored
                for stored in digests
                if any(hmac.compare_digest(stored, candidate) for candidate in candidates)
            )
            found = next(matching, None)
            if found is None:
                return "wrong", None
            # Used once, until the app deletes it: of two requests with it, one wins.
            used = self._key(cache, "recovery", user_id, found)
            token = secrets.token_hex(8)
            if not await guard.run(
                "acquire_lock", lambda: backend.acquire_lock(used, token, 86_400)
            ):
                return "replayed", None
            await self._forgive(cache, user_id)
        except BackendUnavailable as exc:
            logger.warning("Couldn't check a recovery code", exc_info=exc)
            return "unchecked", None
        return "used", found

    def _recovery_digest(self, secret: bytes, user_id: str | int | uuid.UUID, code: str) -> str:
        message = json.dumps(["recovery", str(user_id), _normalized(code)]).encode()
        return hmac.new(secret, message, hashlib.sha256).hexdigest()

    # Step-up: a recent second factor, in the session.

    def mark(self, request: HTTPConnection, user_id: str | int | uuid.UUID) -> None:
        """Note in the session that `user_id` just gave their code (`stepup.mark`), and give
        the session a new id: it holds more privileges now."""
        stepup.mark(request, user_id, "totp")

    def verified_at(self, request: HTTPConnection, user_id: str | int | uuid.UUID) -> float | None:
        """When `user_id` last gave a second factor in this session; None when they didn't, or
        someone else did (`stepup.verified_at`)."""
        return stepup.verified_at(request, user_id)

    def require_recent(
        self,
        user_id: Callable[..., Any],
        max_age: int = 300,
    ) -> Callable[..., Any]:
        """A dependency that needs a second factor (this one, or a passkey) of the last
        `max_age` seconds in this session: `stepup.require_recent`."""
        return stepup.require_recent(user_id, max_age)

    # What they share.

    def _bound(self, user_id: str | int | uuid.UUID) -> str:
        """The associated data of a user's sealed secret: it only opens for them."""
        if isinstance(user_id, bool) or not isinstance(user_id, str | int | uuid.UUID):
            raise TypeError("user_id is an ID: a str, an int or a UUID")
        return f"{self.issuer}\0totp\0{user_id}"

    def _open(self, user_id: str | int | uuid.UUID, sealed: str) -> bytes:
        try:
            if not sealed.startswith(_SEALED_PREFIX):
                raise ValueError
            data = base64.urlsafe_b64decode(sealed[len(_SEALED_PREFIX) :])
        except ValueError:
            data = b""
        key = self._encrypter.open(self._bound(user_id), data)
        if key is None:
            raise ValueError(
                "the sealed secret isn't this user's, or was sealed with another secret"
            )
        return key

    def _key(self, cache: Cache, kind: str, *parts: str) -> str:
        return cache.state_key(self.namespace, f"{kind}:{_digest(*parts)}")

    async def _locked_out(self, cache: Cache, user_id: str) -> bool:
        """Count a guess; whether there have been more than max_attempts since the first one."""
        backend = shared_backend(cache.backend)
        attempts = self._key(cache, "attempts", user_id)
        count = await cache.guard.run("incr", lambda: backend.incr(attempts, 1, self.lockout))
        return count > self.max_attempts

    async def _forgive(self, cache: Cache, user_id: str) -> None:
        """A right code: the guesses start again."""
        backend = shared_backend(cache.backend)
        attempts = self._key(cache, "attempts", user_id)
        try:
            await cache.guard.run("delete", lambda: backend.clear(key=attempts))
        except BackendUnavailable as exc:
            logger.debug("Couldn't reset the guesses at TOTP codes (they expire): %s", exc)

    async def _audit(
        self,
        action: str,
        outcome: str,
        user_id: str | int | uuid.UUID,
        request: HTTPConnection | None,
    ) -> None:
        if self.audit is None:
            return
        success = outcome in ("success", "used")
        await self.audit.log(
            action,
            outcome="success" if success else "failure",
            actor=user_id,
            reason=None if success else outcome,
            request=request,
        )
