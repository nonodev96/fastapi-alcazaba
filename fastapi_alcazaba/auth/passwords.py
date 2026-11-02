"""Passwords: kept as slow hashes, checked against the ones known to be leaked, guessed at most a
few times per account, and reset by a link that works once.

    passwords = FastAPIPasswords(pepper=settings.password_pepper)

    problems = await passwords.check(form.password, user_inputs=[form.email])  # at sign-up
    user.password = await passwords.hash(form.password)

    if not await passwords.verify(user.id, form.password, user.password):  # at login
        raise HTTPException(401, "Wrong e-mail or password")
    if passwords.needs_rehash(user.password):
        user.password = await passwords.hash(form.password)

    token = await passwords.reset_token(user.id)        # in the e-mail's link
    user_id = await passwords.use_reset_token(token)   # once, then a new password

The hashes are scrypt's (RFC 7914), from the standard library, with OWASP's parameters, in PHC's
format (`$scrypt$ln=17,r=8,p=1$<salt>$<hash>`), so that stronger parameters can come later: a
hash made with weaker ones `needs_rehash`.
"""

import base64
import hashlib
import hmac
import importlib.util
import logging
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection

from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.response_cache.caches import Cache, resolve_cache

if TYPE_CHECKING:
    from fastapi_alcazaba.audit import FastAPIAudit

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_PHC = re.compile(
    r"\$scrypt\$ln=(\d{1,2}),r=(\d{1,2}),p=(\d{1,2})\$([A-Za-z0-9+/]+)\$([A-Za-z0-9+/]+)"
)
_SALT_SIZE = 16
_HASH_SIZE = 32
#: The longest password hashed: past it, the hash would cost more than a login should.
MAX_PASSWORD = 1024


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True, slots=True)
class ScryptParameters:
    """scrypt's cost: N = 2^`log_n`, `r`, `p`. OWASP's: 2^17, 8, 1 (about 128 MiB and a fifth of
    a second each)."""

    log_n: int = 17
    r: int = 8
    p: int = 1

    def __post_init__(self) -> None:
        if not (14 <= self.log_n <= 22 and 1 <= self.r <= 32 and 1 <= self.p <= 16):
            raise ValueError("scrypt's parameters: log_n 14 to 22, r 1 to 32, p 1 to 16")

    @property
    def memory(self) -> int:
        """Bytes scrypt needs, with room: what `maxmem` must allow."""
        return int(128 * self.r * (2**self.log_n + self.p + 2))


class FastAPIPasswords:
    """Passwords, from sign-up to reset.

    - **Hashes:** scrypt with a random salt each, in a thread (it takes a fifth of a second on
      purpose); with a `pepper`, the password is first an HMAC of it, so that a stolen database
      without the pepper gives nothing to guess against.
    - **Guesses:** after `max_attempts` wrong passwords for an account, its logins are refused for
      `lockout` seconds, the right password too; the attempts of other accounts aren't affected.
      Put a rate limit per client on the login route too, for one client trying many accounts.
    - **Unknown accounts** take as long as known ones: `verify` hashes against a made-up hash.
    - **Leaked passwords:** with `pwned` (`PwnedPasswords`), `check` says when a password is
      among those leaked, which attackers try first.
    - **Resets:** `reset_token` makes a random token, kept as a digest for `reset_ttl` seconds,
      that `use_reset_token` turns into its user once; a new one replaces the user's last.

    :param parameters: scrypt's cost.
    :param pepper: a secret of 32 bytes or more, kept out of the database (your settings). It
        can't be rotated without the passwords: keep it.
    :param min_length: characters a new password needs (NIST SP 800-63B: 15 when the password is
        the only factor, 8 with another).
    :param max_attempts: wrong passwords per account before the lockout.
    :param lockout: seconds an account's logins are refused after them.
    :param reset_ttl: seconds a reset token lasts.
    :param pwned: checks the passwords against those leaked; None for no check.
    :param cache: the Cache whose backend keeps the attempts and the reset tokens; FastAPICache's
        by default.
    :param namespace: part of the keys, after the cache's prefix (see `Cache.state_key`).
    :param audit: the audit trail that records each password checked (`auth.password`) and each
        reset (`auth.password_reset`), with their outcome.
    """

    def __init__(
        self,
        *,
        parameters: ScryptParameters | None = None,
        pepper: str | SecretStr | None = None,
        min_length: int = 15,
        max_attempts: int = 10,
        lockout: int = 900,
        reset_ttl: int = 3600,
        pwned: "PwnedPasswords | None" = None,
        cache: Cache | None = None,
        namespace: str = "passwords",
        audit: "FastAPIAudit | None" = None,
    ) -> None:
        if pepper is not None:
            pepper = pepper if isinstance(pepper, SecretStr) else SecretStr(pepper)
            if len(pepper.get_secret_value().encode()) < 32:
                raise ValueError("the pepper is a secret of 32 bytes or more")
        if not 8 <= min_length <= 64:
            raise ValueError("min_length is 8 to 64 characters")
        if max_attempts < 1 or lockout < 1 or reset_ttl < 60:
            raise ValueError("max_attempts and lockout are 1 or more, reset_ttl 60 or more")
        self.parameters = parameters or ScryptParameters()
        self._pepper = pepper
        self.min_length = min_length
        self.max_attempts = max_attempts
        self.lockout = lockout
        self.reset_ttl = reset_ttl
        self.pwned = pwned
        self.cache = cache
        self.namespace = namespace
        self.audit = audit
        self._decoy_salt = secrets.token_bytes(_SALT_SIZE)

    # Hashes.

    async def hash(self, password: str) -> str:
        """The hash of `password` to store, in PHC's format. Raises ValueError for a password
        longer than `MAX_PASSWORD`: `check` tells it first."""
        if len(password) > MAX_PASSWORD:
            raise ValueError(f"a password has {MAX_PASSWORD} characters at most")
        salt = secrets.token_bytes(_SALT_SIZE)
        digest = await run_in_threadpool(self._scrypt, password, salt, self.parameters)
        p = self.parameters
        return f"$scrypt$ln={p.log_n},r={p.r},p={p.p}${_b64(salt)}${_b64(digest)}"

    def needs_rehash(self, stored: str) -> bool:
        """Whether `stored` was made with other parameters: hash the password again at the next
        login, when it's at hand."""
        found = _PHC.fullmatch(stored)
        if found is None:
            return True
        made = tuple(int(found.group(index)) for index in (1, 2, 3))
        return made != (self.parameters.log_n, self.parameters.r, self.parameters.p)

    async def verify(
        self,
        user_id: Any,
        password: str,
        stored: str | None,
        *,
        request: HTTPConnection | None = None,
    ) -> bool:
        """Whether `password` is the one of `stored`, the hash of `user_id`'s. False when it
        isn't, when the account is locked out by wrong ones, or when the backend fails. For an
        account that doesn't exist, `stored=None` (and its user_id, or None): it takes as long
        as one that does."""
        outcome = await self._verify(user_id, password, stored)
        resolve_cache(self.cache).metrics.count("passwords", outcome=outcome)
        await self._audit("auth.password", outcome, user_id, request)
        return outcome == "success"

    async def _verify(self, user_id: Any, password: str, stored: str | None) -> str:
        cache = resolve_cache(self.cache)
        backend = shared_backend(cache.backend)
        attempts = self._key(cache, "attempts", user_id)
        try:
            if user_id is not None:
                count = await cache.guard.run(
                    "incr", lambda: backend.incr(attempts, 1, self.lockout)
                )
                if count > self.max_attempts:
                    await self._decoy_check(password)  # as long as a check: nothing to time
                    return "locked"
        except BackendUnavailable as exc:
            logger.warning("Couldn't count the attempts at a password: refusing it", exc_info=exc)
            await self._decoy_check(password)
            return "unchecked"
        if len(password) > MAX_PASSWORD:
            await self._decoy_check(password[:MAX_PASSWORD])
            return "too_long"  # never hashed: no password of an account is that long
        found = _PHC.fullmatch(stored) if stored is not None else None
        if found is None:
            await self._decoy_check(password)
            return "unknown" if stored is None else "malformed"
        try:
            parameters = ScryptParameters(*(int(found.group(index)) for index in (1, 2, 3)))
        except ValueError:
            await self._decoy_check(password)
            return "malformed"
        salt, expected = _unb64(found.group(4)), _unb64(found.group(5))
        digest = await run_in_threadpool(self._scrypt, password, salt, parameters, len(expected))
        if not hmac.compare_digest(digest, expected):
            return "wrong"
        try:
            await cache.guard.run("delete", lambda: backend.clear(key=attempts))
        except BackendUnavailable as exc:
            logger.debug("Couldn't reset the attempts at a password (they expire): %s", exc)
        return "success"

    async def _decoy_check(self, password: str) -> None:
        """A hash as long as a real check, of nothing: an unknown or locked account, timed."""
        await run_in_threadpool(self._scrypt, password, self._decoy_salt, self.parameters)

    def _scrypt(
        self,
        password: str,
        salt: bytes,
        parameters: ScryptParameters,
        size: int = _HASH_SIZE,
    ) -> bytes:
        secret = password.encode()
        if self._pepper is not None:
            key = self._pepper.get_secret_value().encode()
            secret = hmac.new(key, secret, hashlib.sha256).digest()
        return hashlib.scrypt(
            secret,
            salt=salt,
            n=2**parameters.log_n,
            r=parameters.r,
            p=parameters.p,
            maxmem=parameters.memory,
            dklen=size,
        )

    # New passwords.

    async def check(self, password: str, *, user_inputs: Sequence[str] = ()) -> list[str]:
        """What's wrong with `password` as a new one, as NIST SP 800-63B says: "too_short"
        (`min_length`), "too_long" (`MAX_PASSWORD`), "contains_user_input" (one of `user_inputs`,
        such as the username or the e-mail address's local part, of 4 characters or more), and
        "pwned" (among those leaked). No rule on its characters: length is what makes it strong.
        An empty list when it's fine."""
        problems = []
        if len(password) < self.min_length:
            problems.append("too_short")
        if len(password) > MAX_PASSWORD:
            problems.append("too_long")
        lowered = password.casefold()
        for value in user_inputs:
            parts = {value, value.split("@")[0]} if "@" in value else {value}
            if any(len(part) >= 4 and part.casefold() in lowered for part in parts):
                problems.append("contains_user_input")
                break
        if self.pwned is not None and not problems and await self.pwned.count(password) > 0:
            problems.append("pwned")
        return problems

    # Resets.

    async def reset_token(self, user_id: Any) -> str:
        """A token for `user_id` to set a new password, for the link of the e-mail it's sent by;
        it replaces their last one. Raises BackendUnavailable when the backend fails."""
        cache = resolve_cache(self.cache)
        backend = shared_backend(cache.backend)
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        latest = self._key(cache, "reset-of", user_id)
        stored = f"{digest}".encode()
        await cache.guard.run("set", lambda: backend.set(latest, stored, self.reset_ttl))
        record = str(user_id).encode()
        key = self._key(cache, "reset", digest)
        await cache.guard.run("set", lambda: backend.set(key, record, self.reset_ttl))
        return token

    async def use_reset_token(
        self,
        token: str,
        *,
        request: HTTPConnection | None = None,
    ) -> str | None:
        """The user ID (as a string) whose `token` it is, used up; None when it's unknown,
        expired, used, or replaced by a newer one. Then set the new password, and end the user's
        sessions (`UserSessions.end_all`)."""
        outcome, user_id = await self._use_reset(token)
        resolve_cache(self.cache).metrics.count("passwords", outcome=f"reset_{outcome}")
        await self._audit("auth.password_reset", outcome, user_id, request)
        return user_id

    async def _use_reset(self, token: str) -> tuple[str, str | None]:
        if not isinstance(token, str) or not 1 <= len(token) <= 100:
            return "malformed", None
        cache = resolve_cache(self.cache)
        backend = shared_backend(cache.backend)
        digest = hashlib.sha256(token.encode()).hexdigest()
        key = self._key(cache, "reset", digest)
        try:
            stored = await cache.guard.run("get", lambda: backend.get(key))
            if stored is None:
                return "unknown", None
            # Used once: of two requests with the token, the one that deletes it.
            if await cache.guard.run("delete", lambda: backend.clear(key=key)) != 1:
                return "unknown", None
            user_id = stored.decode()
            latest_key = self._key(cache, "reset-of", user_id)
            latest = await cache.guard.run("get", lambda: backend.get(latest_key))
        except BackendUnavailable as exc:
            logger.warning("Couldn't check a password reset token", exc_info=exc)
            return "unchecked", None
        if latest is None or not hmac.compare_digest(latest, digest.encode()):
            return "replaced", None
        return "used", user_id

    # What they share.

    def _key(self, cache: Cache, kind: str, value: Any) -> str:
        digest = hashlib.sha256(f"{kind}\0{value}".encode()).hexdigest()
        return cache.state_key(self.namespace, f"{kind}:{digest}")

    async def _audit(
        self,
        action: str,
        outcome: str,
        user_id: Any,
        request: HTTPConnection | None,
    ) -> None:
        if self.audit is None:
            return
        success = outcome in ("success", "used")
        is_id = isinstance(user_id, str | int) and not isinstance(user_id, bool)
        actor = user_id if is_id else None
        await self.audit.log(
            action,
            outcome="success" if success else "failure",
            actor=actor,
            reason=None if success else outcome,
            request=request,
        )


class PwnedPasswords:
    """Whether a password is among those leaked, by Have I Been Pwned's Pwned Passwords API,
    without sending it: only the first five characters of its SHA-1 go out (k-anonymity), and the
    answer is padded, so that its size doesn't tell them either. Needs httpx2.

    :param timeout: seconds for the call.
    :param fail_open: when the API doesn't answer, take the password as not leaked (the default:
        the check is advice, and a sign-up mustn't wait for it), or raise.
    :param client: the httpx2 client to call it with; one of its own by default.
    :param url: the API's range endpoint.
    """

    def __init__(
        self,
        *,
        timeout: float = 2.0,
        fail_open: bool = True,
        client: Any = None,
        url: str = "https://api.pwnedpasswords.com/range/",
    ) -> None:
        if importlib.util.find_spec("httpx2") is None:  # pragma: no cover
            raise ImportError(
                'PwnedPasswords needs httpx2: pip install "fastapi-alcazaba[passwords]"'
            )
        self.timeout = timeout
        self.fail_open = fail_open
        self.client = client
        self.url = url

    async def count(self, password: str) -> int:
        """How many times `password` is in the leaks; 0 when it isn't (or the API didn't answer,
        with `fail_open`)."""
        import httpx2 as httpx

        sha1 = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()
        prefix, suffix = sha1[:5], sha1[5:]
        headers = {"Add-Padding": "true", "User-Agent": "fastapi-alcazaba"}
        try:
            if self.client is not None:
                response = await self.client.get(
                    self.url + prefix, headers=headers, timeout=self.timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(self.url + prefix, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            if not self.fail_open:
                raise
            logger.warning("Couldn't check a password against the leaks: %s", exc)
            return 0
        for line in response.text.splitlines():
            found, _, times = line.strip().partition(":")
            if found == suffix:
                return int(times) if times.isdigit() else 0
        return 0
