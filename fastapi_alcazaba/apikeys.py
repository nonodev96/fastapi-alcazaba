"""API keys for machine clients: generated here, stored in your database, checked by a dependency.

    keys = FastAPIAPIKeys(load_key, pepper=settings.api_key_pepper)

    # Issuing one: show `new.key` once, store `new.record` (never the key).
    new = keys.generate(scopes=["orders:read"], expires_at=in_90_days)
    await db.save_api_key(owner=user.id, **dataclasses.asdict(new.record))

    async def load_key(key_id: str) -> StoredKey | None:  # your row, or None
        return await db.get_api_key(key_id)

    @app.get("/orders")
    async def orders(key: Annotated[StoredKey, Depends(keys.require("orders:read"))]) -> ...

A key looks like `ak_<id>_<secret>`: the id finds its record, and the record holds a digest of the
secret, not the secret, so a leak of the database doesn't leak working keys. The secret has 256
random bits: a plain SHA-256 digest can't be brute-forced (passwords need a slow hash; random keys
don't), and the check costs microseconds. With a `pepper`, a server secret kept out of the
database, the digest is an HMAC, so the database alone isn't even enough to test a guess.
"""

import base64
import hashlib
import hmac
import inspect
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Generic, NamedTuple, Protocol, TypeVar

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from fastapi_alcazaba.integrity import Secrets, secret_list
from fastapi_alcazaba.metrics import Metrics

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_ID_BYTES = 10  # 80 bits: unique, not secret
_SECRET_BYTES = 32  # 256 bits
_ID_LENGTH = 16  # base32 characters of 10 bytes
_SECRET_LENGTH = 52  # base32 characters of 32 bytes, without padding
_MAX_KEY = 256  # characters read from a request
_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
_SCOPE = re.compile(r"[!#-\[\]-~]+")  # RFC 6749 scope-token: printable ASCII but space, " and \
_CONTEXT = b"fastapi_alcazaba.apikey"


class StoredKey(Protocol):
    """What `lookup` returns: `APIKeyRecord`, or your own row with these attributes."""

    @property
    def digest(self) -> str:
        """Hex digest of the secret, as `generate()` made it."""
        ...

    @property
    def scopes(self) -> Collection[str]:
        """What the key may do; a string is split on spaces."""
        ...

    @property
    def expires_at(self) -> datetime | None:
        """When it stops working (naive datetimes are taken as UTC); None for never."""
        ...


R = TypeVar("R", bound=StoredKey)

#: Finds the record of a key id: async or not (then it runs in a thread); None when unknown.
Lookup = Callable[[str], Awaitable[R | None] | R | None]


@dataclass(frozen=True, slots=True)
class APIKeyRecord:
    """What to store for a key: everything but the key itself."""

    #: Public part of the key: look the record up by it (unique).
    id: str
    #: Hex digest of the secret part.
    digest: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    expires_at: datetime | None = None


class NewAPIKey(NamedTuple):
    """A key just generated: `key` to show once, `record` to store."""

    key: str
    record: APIKeyRecord


def _token(size: int) -> str:
    return base64.b32encode(secrets.token_bytes(size)).decode().rstrip("=").lower()


def _scope_set(scopes: Collection[str]) -> frozenset[str]:
    return frozenset(scopes.split() if isinstance(scopes, str) else scopes)


def _is_async(function: object) -> bool:
    return inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(
        getattr(function, "__call__", None)  # noqa: B004  # objects with an async __call__
    )


class FastAPIAPIKeys(Generic[R]):
    """FastAPI dependency that authenticates a request by its API key and returns its record.

        keys = FastAPIAPIKeys(load_key, pepper=settings.api_key_pepper)

        @app.get("/me")
        async def me(key: Annotated[StoredKey, Depends(keys)]) -> ...

        @app.delete("/orders/{id}", dependencies=[Depends(keys.require("orders:write"))])

    The key comes in the `header` (`X-API-Key` by default) or, with `bearer=True`, as
    `Authorization: Bearer <key>`; both appear in the OpenAPI schema, for the docs' Authorize
    button. A missing, malformed, unknown, wrong or expired key gets the same 401 (with
    `WWW-Authenticate`); a valid key without a scope that `require` asks for gets a 403. Keys
    and secrets are never logged; put a FastAPIRateLimiter in front, since each request with a
    well-formed key costs a `lookup`.

    Revoking a key: delete its record, or set its `expires_at` to the past. With `cache_ttl`,
    records are kept in each process for that many seconds, so a revocation takes up to that
    long to reach every process.

    :param lookup: `lookup(key_id)`: the stored record of that id, or None. Async, or a plain
        function, which runs in a thread like FastAPI's sync dependencies.
    :param header: the request header holding the key.
    :param prefix: the start of every key (`ak_...`): letters, digits and underscores, starting
        with a letter. Secret scanners (GitHub's, GitLab's) can be taught to spot it.
    :param bearer: also accept the key as `Authorization: Bearer <key>`.
    :param pepper: a server secret (at least 32 bytes) mixed into every digest, kept out of the
        database (an environment variable, a secret manager). A list rotates it: the first one
        is used for new keys, all of them are accepted; old peppers must stay as long as keys
        made with them do. Changing it without keeping the old one invalidates every key.
    :param cache_ttl: seconds each process keeps a record it looked up; 0 looks up every request.
    :param cache_size: records kept at most per process (the least recently used are dropped).
    :param scheme_name: name of the scheme in the OpenAPI schema.
    :param metrics: where to count the outcomes (`fastapi_alcazaba.api_keys`); FastAPICache's by
        default.
    """

    def __init__(
        self,
        lookup: "Lookup[R]",
        *,
        header: str = "X-API-Key",
        prefix: str = "ak",
        bearer: bool = False,
        pepper: Secrets | None = None,
        cache_ttl: float = 0.0,
        cache_size: int = 1_024,
        scheme_name: str = "APIKey",
        metrics: Metrics | None = None,
    ) -> None:
        if not _PREFIX.fullmatch(prefix) or prefix.endswith("_"):
            raise ValueError(
                "prefix must be 1 to 32 letters, digits and underscores, starting with a letter "
                "and not ending with an underscore"
            )
        if cache_ttl < 0:
            raise ValueError("cache_ttl can't be negative")
        if cache_size < 1:
            raise ValueError("cache_size must be at least 1")
        self.lookup = lookup
        self.header = header
        self.prefix = prefix
        self.bearer = bearer
        self.cache_ttl = cache_ttl
        self.cache_size = cache_size
        # A key of their own: the pepper may protect other things too.
        self._peppers = (
            [hmac.new(key, _CONTEXT, hashlib.sha256).digest() for key in secret_list(pepper)]
            if pepper is not None
            else None
        )
        self._pattern = re.compile(
            rf"{re.escape(prefix)}_([a-z2-7]{{{_ID_LENGTH}}})_([a-z2-7]{{{_SECRET_LENGTH}}})"
        )
        self._is_async = _is_async(lookup)
        self._challenge = "Bearer" if bearer else "APIKey"
        self._metrics = metrics
        self._records: OrderedDict[str, tuple[float, R]] = OrderedDict()
        parameters = [
            inspect.Parameter(
                "api_key",
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=Annotated[
                    str | None,
                    Security(APIKeyHeader(name=header, scheme_name=scheme_name, auto_error=False)),
                ],
            )
        ]
        if bearer:
            parameters.append(
                inspect.Parameter(
                    "authorization",
                    inspect.Parameter.KEYWORD_ONLY,
                    default=None,
                    annotation=Annotated[
                        HTTPAuthorizationCredentials | None,
                        Security(HTTPBearer(scheme_name=f"{scheme_name}Bearer", auto_error=False)),
                    ],
                )
            )
        # What FastAPI reads to know where the key comes from.
        self.__signature__ = inspect.Signature(parameters, return_annotation=Any)

    async def __call__(
        self,
        *,
        api_key: str | None = None,
        authorization: HTTPAuthorizationCredentials | None = None,
    ) -> R:
        if not api_key and authorization is not None and self.bearer:
            api_key = authorization.credentials
        return await self.authenticate(api_key)

    def require(self, *scopes: str) -> Callable[..., Awaitable[R]]:
        """A dependency like this one that also needs every scope in `scopes` (a 403 without
        them): `Depends(keys.require("orders:write"))`."""
        needed = frozenset(scopes)
        if not needed or not all(_SCOPE.fullmatch(scope) for scope in needed):
            raise ValueError("give one or more scopes, of printable ASCII without spaces")
        challenge = f'Bearer error="insufficient_scope", scope="{" ".join(sorted(needed))}"'

        async def with_scopes(record: Annotated[Any, Depends(self)]) -> R:
            if not needed <= _scope_set(record.scopes):
                headers = {"WWW-Authenticate": challenge} if self.bearer else None
                self._count("insufficient_scope")
                raise HTTPException(403, "Insufficient scope", headers=headers)
            return record  # type: ignore[no-any-return]

        return with_scopes

    def generate(
        self,
        *,
        scopes: Iterable[str] = (),
        expires_at: datetime | None = None,
    ) -> NewAPIKey:
        """A new key, to show its owner once, and its record, to store. `scopes` are free-form
        names ("orders:read"); `expires_at` a datetime with its time zone."""
        granted = frozenset(scopes)
        if not all(_SCOPE.fullmatch(scope) for scope in granted):
            raise ValueError("scopes must be printable ASCII without spaces")
        if expires_at is not None and expires_at.utcoffset() is None:
            raise ValueError("naive datetimes are ambiguous: give it a time zone (e.g. UTC)")
        key_id, secret = _token(_ID_BYTES), _token(_SECRET_BYTES)
        record = APIKeyRecord(
            id=key_id,
            digest=self._digests(key_id, secret)[0],
            scopes=granted,
            expires_at=expires_at,
        )
        return NewAPIKey(f"{self.prefix}_{key_id}_{secret}", record)

    async def authenticate(self, key: str | None) -> R:
        """The record of `key`, if it's valid; raises an HTTPException (401) otherwise. For keys
        that come some other way (a WebSocket message, a test)."""
        parsed = self._pattern.fullmatch(key) if key and len(key) <= _MAX_KEY else None
        if parsed is None:
            raise self._unauthorized("malformed")
        key_id, secret = parsed.groups()
        # Only digests from here on: the secret isn't around while `lookup` runs (and raises).
        candidates = self._digests(key_id, secret)
        del key, parsed, secret
        record = await self._record(key_id)
        if record is None:
            logger.debug("API key %s refused: unknown", key_id)
            raise self._unauthorized("unknown")
        stored = record.digest.encode()
        if not any(hmac.compare_digest(stored, candidate.encode()) for candidate in candidates):
            logger.info("API key %s refused: wrong secret", key_id)
            raise self._unauthorized("wrong_secret")
        expires_at = record.expires_at
        if expires_at is not None:
            if expires_at.utcoffset() is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if datetime.now(UTC) >= expires_at:
                logger.info("API key %s refused: expired", key_id)
                raise self._unauthorized("expired")
        self._count("accepted")
        return record

    def forget(self, key_id: str | None = None) -> None:
        """Drop the record of `key_id` (every record when None) from this process's cache: call
        it after revoking a key, for this process to see it at once."""
        if key_id is None:
            self._records.clear()
        else:
            self._records.pop(key_id, None)

    def _digests(self, key_id: str, secret: str) -> list[str]:
        """The digest of a key with each pepper (the first one for new keys)."""
        # Bound to the id: a digest copied to another record doesn't work there.
        material = f"{key_id}\0{secret}".encode()
        if self._peppers is None:
            return [hashlib.sha256(_CONTEXT + b"\0" + material).hexdigest()]
        return [hmac.new(pepper, material, hashlib.sha256).hexdigest() for pepper in self._peppers]

    async def _record(self, key_id: str) -> R | None:
        if self.cache_ttl:
            cached = self._records.get(key_id)
            if cached is not None:
                until, hit = cached
                if time.monotonic() < until:
                    self._records.move_to_end(key_id)
                    return hit
                del self._records[key_id]
        if self._is_async:
            found = self.lookup(key_id)
        else:
            found = await run_in_threadpool(self.lookup, key_id)
        if inspect.isawaitable(found):
            found = await found
        record: R | None = found
        if record is not None and self.cache_ttl:
            # Only records: a flood of unknown ids would push the useful ones out.
            self._records[key_id] = (time.monotonic() + self.cache_ttl, record)
            if len(self._records) > self.cache_size:
                self._records.popitem(last=False)
        return record

    def _unauthorized(self, outcome: str) -> HTTPException:
        """The same 401 whatever was wrong; only the metrics tell."""
        self._count(outcome)
        return HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": self._challenge})

    def _count(self, outcome: str) -> None:
        if self._metrics is not None:
            metrics = self._metrics
        else:
            from fastapi_alcazaba import FastAPICache

            metrics = FastAPICache.get_metrics()
        metrics.count("api_keys", outcome=outcome)
