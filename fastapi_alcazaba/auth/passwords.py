"""Passwords: kept as slow hashes, checked against the ones known to be leaked, guessed at most a
few times per account, and reset by a link that works once.

    passwords = FastAPIPasswords(
        pepper=settings.password_pepper,
        strength=PasswordStrength(min_score=3),  # zxcvbn: the password-strength extra
        blocklist=PasswordBlocklist.from_files("banned.txt"),
        pwned=PwnedPasswords(),  # Have I Been Pwned: the passwords extra
    )

    result = await passwords.evaluate(form.password, user_inputs=[form.email])  # at sign-up
    if not result.ok:
        raise HTTPException(422, {"password": result.problems})
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
import math
import os
import re
import secrets
import threading
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

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
#: What `FastAPIPasswords.evaluate` can find wrong with a new password, in the order it looks.
PROBLEMS = (
    "too_short",
    "too_long",
    "invalid_characters",
    "contains_user_input",
    "blocklisted",
    "weak",
    "pwned",
)
#: A line of a Pwned Passwords range answer: a SHA-1's last 35 hex digits and how many times.
_RANGE_LINE = re.compile(r"([0-9A-Fa-f]{35}):([0-9]{1,12})")
#: What separates the words of a name or an e-mail address, for zxcvbn.
_WORDS = re.compile(r"[\s@._+-]+")
#: zxcvbn keeps the user inputs of a call in a module global: one call at a time.
_ZXCVBN_LOCK = threading.Lock()


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


def _utf8(password: str) -> bytes:
    """The password's bytes. A lone surrogate (valid in JSON, not in UTF-8) is kept as its own
    bytes instead of raising: no password stored before could hold one."""
    return password.encode("utf-8", "surrogatepass")


def _is_text(password: str) -> bool:
    try:
        password.encode()
    except UnicodeEncodeError:
        return False
    return True


def _user_input_parts(user_inputs: Sequence[str]) -> list[str]:
    """The user's inputs, and an e-mail address's local part too."""
    if isinstance(user_inputs, str):  # a str is a Sequence[str] too: its characters
        raise TypeError("user_inputs is a list of strings, such as [email, name]")
    parts = []
    for value in user_inputs:
        parts.append(value)
        if "@" in value:
            parts.append(value.split("@")[0])
    return parts


def _unaccented(text: str) -> str:
    """`text` without its accents: "Zurbarán" is also typed "zurbaran"."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _normalize(password: str) -> str:
    """How a blocklist compares passwords: NFKC (fullwidth and compatibility forms as their plain
    letters), casefolded, without spaces around."""
    return unicodedata.normalize("NFKC", password).casefold().strip()


@dataclass(frozen=True, slots=True)
class PasswordCheck:
    """What `FastAPIPasswords.evaluate` found about a new password. It never holds the password.

    :param problems: why it's refused, from `PROBLEMS`; empty when it's accepted.
    :param score: zxcvbn's, 0 (too guessable) to 4 (very unguessable); None without a
        `PasswordStrength`.
    :param checks: those made: "length", "characters", and "user_inputs", "blocklist",
        "strength" and "pwned" when they're set up (and, for "pwned", when the others passed).
    :param unavailable: those that couldn't be made: "pwned" when the API didn't answer and the
        check fails open.
    :param suggestions: zxcvbn's advice for a better one, in English, for the user.
    """

    problems: tuple[str, ...] = ()
    score: int | None = None
    checks: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the password is accepted."""
        return not self.problems


class PasswordStrength:
    """How hard a password is to guess, by zxcvbn: it finds dictionary words, names, keyboard
    walks, dates, repeats, sequences and l33t substitutions, and the user's own data, and scores
    the guesses they'd take from 0 to 4. Needs zxcvbn: the `password-strength` extra.

    zxcvbn measures 72 characters at most: a longer password has its first 72 scored. It takes
    a millisecond or so, up to some tens: `FastAPIPasswords` runs it in a thread.

    :param min_score: the lowest score accepted: 3 by default ("safely unguessable", 10^10
        guesses), 4 for accounts that matter more, 2 at the least.
    """

    #: The longest password zxcvbn measures.
    MAX_LENGTH = 72

    def __init__(self, *, min_score: int = 3) -> None:
        if type(min_score) is not int or not 0 <= min_score <= 4:
            raise ValueError("min_score is 0 to 4")
        try:
            from zxcvbn import zxcvbn
        except ImportError:
            raise ImportError(
                'PasswordStrength needs zxcvbn: pip install "fastapi-alcazaba[password-strength]"'
            ) from None
        self._zxcvbn = zxcvbn
        self.min_score = min_score

    def measure(
        self,
        password: str,
        *,
        user_inputs: Sequence[str] = (),
    ) -> tuple[int, tuple[str, ...]]:
        """`password`'s score, 0 to 4, and zxcvbn's advice (a warning and suggestions, empty for
        a good one). `user_inputs`, such as the user's name and e-mail address, count as words
        an attacker knows, with their words, and without accents too. Blocks for its time: in a
        coroutine, run it in a thread."""
        inputs = _user_input_parts(user_inputs)
        inputs += [word for value in user_inputs for word in _WORDS.split(value) if word]
        inputs += [plain for value in inputs if (plain := _unaccented(value)) != value]
        with _ZXCVBN_LOCK:
            result = self._zxcvbn(password[: self.MAX_LENGTH], user_inputs=inputs)
        feedback = result["feedback"]
        advice = [feedback["warning"], *feedback["suggestions"]]
        return int(result["score"]), tuple(text for text in advice if text)


class PasswordBlocklist:
    """Passwords refused whatever their strength: the app's name, its products, a list of common
    ones. Built once, at start-up, and kept in memory (a hundred bytes or so per entry: for
    lists of millions, `PwnedPasswords`).

    Entries and passwords are compared normalized (NFKC, casefolded, without spaces around), so
    that "Acme2024", "ACME2024 " and its fullwidth form are the same; the password that's hashed
    is still the one sent. Only whole passwords match: zxcvbn finds those that hold a word.

    :param passwords: the entries; blank ones are skipped.
    """

    def __init__(self, passwords: Iterable[str]) -> None:
        self._entries = frozenset(found for value in passwords if (found := _normalize(value)))

    @classmethod
    def from_files(cls, *paths: str | os.PathLike[str], encoding: str = "utf-8") -> Self:
        """A blocklist of the files' lines, one password each (SecLists' and NCSC's lists are in
        that format). Raises OSError or UnicodeDecodeError at start-up for a file it can't read."""

        def lines() -> Iterator[str]:
            for path in paths:
                with open(path, encoding=encoding) as file:
                    yield from file

        return cls(lines())

    def __contains__(self, password: object) -> bool:
        return isinstance(password, str) and _normalize(password) in self._entries

    def __len__(self) -> int:
        return len(self._entries)


class FastAPIPasswords:
    """Passwords, from sign-up to reset.

    - **Hashes:** scrypt with a random salt each, in a thread (it takes a fifth of a second on
      purpose); with a `pepper`, the password is first an HMAC of it, so that a stolen database
      without the pepper gives nothing to guess against.
    - **Guesses:** after `max_attempts` wrong passwords for an account, its logins are refused for
      `lockout` seconds, the right password too; the attempts of other accounts aren't affected.
      Put a rate limit per client on the login route too, for one client trying many accounts.
    - **Unknown accounts** take as long as known ones: `verify` hashes against a made-up hash.
    - **New passwords:** `evaluate` says what's wrong with one, as NIST SP 800-63B says: too
      short or long, made of the user's data, and with each check set up, in the app's
      `blocklist` (`PasswordBlocklist`), too easy to guess (`strength`, `PasswordStrength`), or
      among those leaked, which attackers try first (`pwned`, `PwnedPasswords`).
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
    :param strength: refuses new passwords zxcvbn scores below its `min_score`; None for no
        check.
    :param blocklist: refuses new passwords in it; None for no check.
    :param pwned: refuses new passwords among those leaked; None for no check (and no call).
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
        strength: PasswordStrength | None = None,
        blocklist: PasswordBlocklist | None = None,
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
        self.strength = strength
        self.blocklist = blocklist
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
        secret = _utf8(password)
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

    async def evaluate(self, password: str, *, user_inputs: Sequence[str] = ()) -> PasswordCheck:
        """What's wrong with `password` as a new one, as NIST SP 800-63B says, and what was
        checked. Its `problems`, in `PROBLEMS`' order:

        - "too_short" (fewer than `min_length` characters), "too_long" (more than
          `MAX_PASSWORD`);
        - "invalid_characters": a lone surrogate, which JSON lets through but no text holds;
        - "contains_user_input": one of `user_inputs` (the username, the name, the e-mail
          address or its local part) or one of their words, of 4 characters or more, is in it,
          whatever its case and accents;
        - "blocklisted": in `blocklist`;
        - "weak": `strength` scores it below its `min_score`;
        - "pwned": among those leaked, asked only when there's no other problem.

        No rule on its characters (a digit, a capital): length is what makes it strong. Never
        raises for a `str`, but `BackendUnavailable` from a `PwnedPasswords` that fails closed.
        """
        problems, checks = self._local_problems(password, user_inputs)
        score: int | None = None
        suggestions: tuple[str, ...] = ()
        if self.strength is not None:
            checks.append("strength")
            measured, suggestions = await run_in_threadpool(
                self.strength.measure, password, user_inputs=user_inputs
            )
            score = measured
            if measured < self.strength.min_score:
                problems.append("weak")
        unavailable: tuple[str, ...] = ()
        if self.pwned is not None and not problems:
            leaks = await self.pwned.lookup(password)
            if leaks is None:
                unavailable = ("pwned",)
            else:
                checks.append("pwned")
                if leaks > 0:
                    problems.append("pwned")
        return PasswordCheck(
            problems=tuple(problems),
            score=score,
            checks=tuple(checks),
            unavailable=unavailable,
            suggestions=suggestions,
        )

    async def check(self, password: str, *, user_inputs: Sequence[str] = ()) -> list[str]:
        """The problems `evaluate` finds with `password` as a new one; an empty list when it's
        fine."""
        return list((await self.evaluate(password, user_inputs=user_inputs)).problems)

    def _local_problems(
        self,
        password: str,
        user_inputs: Sequence[str],
    ) -> tuple[list[str], list[str]]:
        """The problems found without zxcvbn or the network, and the checks made."""
        problems = []
        checks = ["length", "characters"]
        if len(password) < self.min_length:
            problems.append("too_short")
        if len(password) > MAX_PASSWORD:
            problems.append("too_long")
        if not _is_text(password):
            problems.append("invalid_characters")
        if parts := _user_input_parts(user_inputs):
            checks.append("user_inputs")
            # Each input, an e-mail's local part, and their words, without case or accents.
            words = {word for part in parts for word in (part, *_WORDS.split(part.split("@")[0]))}
            plain = _unaccented(password.casefold())
            if any(len(word) >= 4 and _unaccented(word.casefold()) in plain for word in words):
                problems.append("contains_user_input")
        if self.blocklist is not None:
            checks.append("blocklist")
            if password in self.blocklist:
                problems.append("blocklisted")
        return problems, checks

    # Resets.

    async def reset_token(self, user_id: Any) -> str:
        """A token for `user_id` to set a new password, for the link of the e-mail it's sent by;
        it replaces their last one. Raises BackendUnavailable when the backend fails."""
        cache = resolve_cache(self.cache)
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        await self._put(cache, self._key(cache, "reset-of", user_id), digest.encode())
        await self._put(cache, self._key(cache, "reset", digest), str(user_id).encode())
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
            stored = await self._get(cache, key)
            if stored is None:
                return "unknown", None  # none, or one this app didn't write
            # Used once: of two requests with the token, the one that deletes it.
            if await cache.guard.run("delete", lambda: backend.clear(key=key)) != 1:
                return "unknown", None
            user_id = stored.decode()
            latest = await self._get(cache, self._key(cache, "reset-of", user_id))
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

    async def _put(self, cache: Cache, key: str, payload: bytes) -> None:
        """Stores a reset record, sealed with the cache's protector: whoever can write to the
        backend can't plant one this app would take as its own."""
        protector = cache.options.protector
        value = protector.seal(key, payload) if protector is not None else payload
        backend = shared_backend(cache.backend)
        await cache.guard.run("set", lambda: backend.set(key, value, self.reset_ttl))

    async def _get(self, cache: Cache, key: str) -> bytes | None:
        """A reset record; None when there's none, or it isn't one this app sealed."""
        backend = shared_backend(cache.backend)
        data = await cache.guard.run("get", lambda: backend.get(key))
        protector = cache.options.protector
        if data is None or protector is None:
            return data
        return protector.open(key, data)

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


def _range_count(text: str, suffix: str) -> int:
    """How many times the SHA-1 ending in `suffix` is in a Pwned Passwords range answer: 0 when
    it isn't. Raises ValueError for an answer that isn't one (an error page, a proxy's), so that
    it doesn't pass for "not leaked"."""
    count, lines = 0, 0
    for line in text.splitlines():
        if not (line := line.strip()):
            continue
        found = _RANGE_LINE.fullmatch(line)
        if found is None:
            raise ValueError("not a Pwned Passwords range answer")
        lines += 1
        if found.group(1).upper() == suffix:
            count = int(found.group(2))
    if lines == 0:
        raise ValueError("an empty Pwned Passwords range answer")
    return count


class PwnedPasswords:
    """Whether a password is among those leaked, by Have I Been Pwned's Pwned Passwords API,
    without sending it: only the first five characters of its SHA-1 go out (k-anonymity), and the
    answer is padded, so that its size doesn't tell them either. Needs httpx2: the `passwords`
    extra.

    When the API doesn't answer in `timeout`, answers an error, or answers something that isn't a
    range answer, the check is unavailable: with `fail_open` the password is taken as not leaked
    and a warning logged (with the error's class only: never the password, its hash or the
    prefix); without it, `BackendUnavailable` is raised.

    :param timeout: seconds for the call.
    :param fail_open: when the check is unavailable, accept the password (the default: the check
        is advice, and a sign-up mustn't wait for a third party), or raise `BackendUnavailable`
        (for apps that would rather answer a 503 than take a password unchecked).
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
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout is a number of seconds above 0")
        self.timeout = timeout
        self.fail_open = fail_open
        self.client = client
        self.url = url

    async def count(self, password: str) -> int:
        """How many times `password` is in the leaks; 0 when it isn't (or the check was
        unavailable, with `fail_open`)."""
        return await self.lookup(password) or 0

    async def lookup(self, password: str) -> int | None:
        """How many times `password` is in the leaks, 0 when it isn't; None when the check was
        unavailable, with `fail_open`. Raises `BackendUnavailable` when it was, without."""
        import httpx2 as httpx

        sha1 = hashlib.sha1(_utf8(password), usedforsecurity=False).hexdigest().upper()
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
            return _range_count(response.text, suffix)
        except (httpx.HTTPError, ValueError) as exc:
            # The class only: httpx's messages hold the URL, and so the hash's prefix.
            reason = type(exc).__name__
            if not self.fail_open:
                raise BackendUnavailable(f"Pwned Passwords is unavailable: {reason}") from None
            logger.warning("Couldn't check a password against the leaks: %s", reason)
            return None
