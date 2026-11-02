"""API keys managed by [Unkey](https://www.unkey.com), through its API (v2): `UnkeyStrategy`
verifies the key of each request, and `UnkeyKeys` issues and revokes them
(`pip install "fastapi-alcazaba[auth-strategy-unkey]"`).

    verifier = UnkeyClient(settings.unkey_verify_key)  # a root key that may only verify
    auth.use(UnkeyStrategy(verifier, permissions="documents.read"))

    keys = UnkeyKeys(UnkeyClient(settings.unkey_admin_key), api_id=settings.unkey_api_id)
    created = await keys.create(external_id=user.id, credits_left=1_000)

Unkey keeps the keys (as hashes), their permissions, credits and rate limits, and answers each
verification: a key that doesn't exist, is disabled or expired fails the authentication (a 401),
one without the permissions gets a 403, and one over its rate limit or out of credits a 429.
When Unkey doesn't answer, the request gets a 503: never let through.
"""

import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NoReturn
from urllib.parse import urlsplit

from fastapi import HTTPException, WebSocketException
from fastapi.security import APIKeyHeader, HTTPBearer
from pydantic import SecretStr
from starlette.requests import HTTPConnection
from starlette.status import (
    HTTP_402_PAYMENT_REQUIRED,
    HTTP_403_FORBIDDEN,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_503_SERVICE_UNAVAILABLE,
    WS_1008_POLICY_VIOLATION,
    WS_1013_TRY_AGAIN_LATER,
)

from fastapi_alcazaba.auth.strategies.base import AuthenticationFailed, Strategy, call
from fastapi_alcazaba.telemetry.metrics import Metrics

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover  # httpx under its old name
    try:
        import httpx  # type: ignore[no-redef,import-not-found]
    except ImportError as exc:
        raise ImportError(
            'UnkeyStrategy needs httpx2: pip install "fastapi-alcazaba[auth-strategy-unkey]"'
        ) from exc

from fastapi_alcazaba.outbound.transports import ResilientTransport

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: Unkey's API.
UNKEY_URL = "https://api.unkey.com"
#: The longest key read from a request: Unkey's own limit.
MAX_KEY = 512
# What a key is made of: Unkey's are a prefix, "_" and base58; nothing that isn't a token.
_KEY = re.compile(r"[A-Za-z0-9_\-.~+/=]{1,512}")
_API_ID = re.compile(r"[A-Za-z0-9_]{3,255}")
_PREFIX = re.compile(r"[A-Za-z0-9_]{1,16}")
_EXTERNAL_ID = re.compile(r"[A-Za-z0-9_.\-]{1,255}")
_KEY_ID = re.compile(r"[A-Za-z0-9_]{3,255}")
# Unkey's answers are a few kilobytes: more is something else answering.
_MAX_RESPONSE = 1024 * 1024
# The codes that mean the key isn't a valid one: authentication fails.
_INVALID = {"NOT_FOUND", "DISABLED", "EXPIRED"}


class UnkeyError(RuntimeError):
    """Unkey refused a call: a root key without the permission, a request it can't take. A
    mistake to fix, not a refusal of the key: a 500.

    :param status: the HTTP status Unkey answered.
    :param detail: Unkey's explanation.
    :param request_id: Unkey's ID of the request, for its support.
    """

    def __init__(self, status: int | None, detail: str, request_id: str | None = None) -> None:
        super().__init__(f"Unkey answered {status}: {detail} (request {request_id})")
        self.status = status
        self.detail = detail
        self.request_id = request_id


class UnkeyUnavailable(UnkeyError):
    """Unkey didn't answer, answered too slowly, with a 5xx, or limited the workspace (a 429):
    the requests are refused with a 503."""


def _check_url(url: str) -> str:
    parts = urlsplit(url)
    local = parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")
    if not (parts.scheme == "https" or local) or not parts.hostname:
        raise ValueError(f"Unkey's URL is https:// (or http://localhost), not {url!r}")
    if parts.query or parts.fragment:
        raise ValueError(f"Unkey's URL has no query nor fragment: {url!r}")
    return url.rstrip("/")


class UnkeyClient:
    """Calls Unkey's API with a root key. Each call has a deadline, and Unkey a circuit breaker:
    when it's down, calls fail at once instead of each waiting for it.

    Give each use its own root key, with only the permissions it needs: one that may only verify
    keys for `UnkeyStrategy`, another that may create and delete them for `UnkeyKeys`.

    :param root_key: the root key (a `SecretStr` from your settings).
    :param url: Unkey's API.
    :param timeout: seconds for each call, the answer included.
    :param client: the httpx2 client to call it with (a proxy, a transport of your own); one of its
        own by default, with a `ResilientTransport`. `aclose()` closes only its own.
    """

    def __init__(
        self,
        root_key: str | SecretStr,
        *,
        url: str = UNKEY_URL,
        timeout: float = 2.0,
        client: "httpx.AsyncClient | None" = None,
    ) -> None:
        self.root_key = root_key if isinstance(root_key, SecretStr) else SecretStr(root_key)
        if not self.root_key.get_secret_value():
            raise ValueError("the root key is empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.url = _check_url(url)
        self.timeout = timeout
        self._own_client = client is None
        self.client = (
            client
            if client is not None
            else httpx.AsyncClient(
                transport=ResilientTransport(timeout=timeout, max_response_size=_MAX_RESPONSE)
            )
        )

    async def call(
        self,
        operation: str,
        body: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        """`POST /v2/<operation>` with `body`: Unkey's `data`, and its ID of the request. Raises
        UnkeyUnavailable when it doesn't answer (or answers 5xx or 429), and UnkeyError when it
        refuses the call."""
        headers = {"Authorization": f"Bearer {self.root_key.get_secret_value()}"}
        try:
            response = await self.client.post(
                f"{self.url}/v2/{operation}", json=body, headers=headers, timeout=self.timeout
            )
        except httpx.TransportError as exc:  # refused, timed out, its circuit breaker open
            raise UnkeyUnavailable(None, f"didn't answer: {exc!r}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            payload = {}
        meta, error = payload.get("meta"), payload.get("error")
        request_id = meta.get("requestId") if isinstance(meta, dict) else None
        if response.status_code >= 500 or response.status_code == 429:
            raise UnkeyUnavailable(response.status_code, _detail(error, response), request_id)
        if response.status_code != 200:
            raise UnkeyError(response.status_code, _detail(error, response), request_id)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise UnkeyError(200, f"{self.url} didn't answer as Unkey does", request_id)
        return data, request_id

    async def aclose(self) -> None:
        """Closes the client it made (not one it was given): in the lifespan, at shutdown."""
        if self._own_client:
            await self.client.aclose()


def _detail(error: Any, response: "httpx.Response") -> str:
    detail = error.get("detail") if isinstance(error, dict) else None
    if isinstance(detail, str):
        return detail
    return response.text[:200] or response.reason_phrase


@dataclass(frozen=True, slots=True)
class RatelimitState:
    """A rate limit of a key, as its verification left it.

    :param name: the limit's name.
    :param limit: requests per window.
    :param remaining: requests left in the window.
    :param reset: seconds until the window starts again.
    :param exceeded: whether this request went past it.
    """

    name: str
    limit: int
    remaining: int
    reset: float
    exceeded: bool


@dataclass(frozen=True, slots=True)
class UnkeyKey:
    """A key Unkey verified, what `UnkeyStrategy` gives `verify` (or the endpoint).

    :param key_id: Unkey's ID of the key, to update or delete it.
    :param name: the key's name.
    :param external_id: the ID of its identity in your app (the user, the tenant), when it has one.
    :param meta: what was stored with the key.
    :param permissions: its permissions, those of its roles included (when some were checked).
    :param roles: its roles (when permissions were checked).
    :param credits: the credits left; None when it has no limit.
    :param expires: when it expires; None when it doesn't.
    :param ratelimits: its rate limits, as this verification left them.
    :param request_id: Unkey's ID of the verification, for its support.
    """

    key_id: str
    name: str | None = None
    external_id: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)
    permissions: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    credits: int | None = None
    expires: datetime | None = None
    ratelimits: tuple[RatelimitState, ...] = ()
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class KeyRatelimit:
    """A rate limit to give a new key: `limit` requests per `duration`.

    :param name: what verifications name it by (3 to 128 characters).
    :param limit: requests per window.
    :param duration: the window, a second or more.
    :param auto_apply: applied to every verification, without naming it.
    """

    name: str
    limit: int
    duration: timedelta
    auto_apply: bool = True

    def __post_init__(self) -> None:
        if not 3 <= len(self.name) <= 128:
            raise ValueError("a rate limit's name has 3 to 128 characters")
        if self.limit < 1:
            raise ValueError("a rate limit allows 1 request or more")
        if self.duration < timedelta(seconds=1):
            raise ValueError("a rate limit's window lasts a second or more")


@dataclass(frozen=True, slots=True)
class CreditRefill:
    """Credits given back to a key every day or month.

    :param amount: the credits it gets back.
    :param interval: daily or monthly.
    :param day: for monthly refills, the day of the month (1 to 31).
    """

    amount: int
    interval: Literal["daily", "monthly"] = "monthly"
    day: int | None = None

    def __post_init__(self) -> None:
        if self.amount < 1:
            raise ValueError("a refill gives 1 credit or more")
        if self.interval not in ("daily", "monthly"):
            raise ValueError("a refill is daily or monthly")
        if self.day is not None and (self.interval != "monthly" or not 1 <= self.day <= 31):
            raise ValueError("the day of a refill is for monthly ones, 1 to 31")


@dataclass(frozen=True, slots=True)
class CreatedKey:
    """A key `UnkeyKeys.create` made.

    :param key: the key, to show its owner once: Unkey only keeps its hash.
    :param key_id: Unkey's ID of the key, to keep, to delete it later.
    """

    key: SecretStr
    key_id: str


def _key_of(data: Mapping[str, Any], request_id: str | None) -> UnkeyKey:
    identity = data.get("identity")
    meta = data.get("meta")
    expires = data.get("expires")
    states = []
    for limit in data.get("ratelimits") or ():
        states.append(
            RatelimitState(
                name=str(limit.get("name", "")),
                limit=int(limit.get("limit", 0)),
                remaining=int(limit.get("remaining", 0)),
                reset=int(limit.get("reset", 0)) / 1000,
                exceeded=bool(limit.get("exceeded", False)),
            )
        )
    return UnkeyKey(
        key_id=str(data.get("keyId", "")),
        name=data.get("name"),
        external_id=identity.get("externalId") if isinstance(identity, dict) else None,
        meta=meta if isinstance(meta, dict) else {},
        permissions=tuple(data.get("permissions") or ()),
        roles=tuple(data.get("roles") or ()),
        credits=data.get("credits"),
        expires=datetime.fromtimestamp(expires / 1000, UTC) if isinstance(expires, int) else None,
        ratelimits=tuple(states),
        request_id=request_id,
    )


class UnkeyStrategy(Strategy):
    """Authenticates by an API key that Unkey verifies: `Authorization: Bearer <key>` by default,
    or the header `header`. Each request is one verification, which spends the key's credits and
    counts in its rate limits: nothing is cached, so a key deleted or disabled is refused at once
    (Unkey takes up to 30 seconds to spread a deletion).

    What Unkey answers:

    - `VALID`: the user is `verify(key)` (an `UnkeyKey`), the key itself without `verify`.
    - `NOT_FOUND`, `DISABLED`, `EXPIRED`: authentication fails (a 401, or the next strategy).
    - `INSUFFICIENT_PERMISSIONS`, `FORBIDDEN`: a 403; the key is valid, it may not do this.
    - `RATE_LIMITED`: a 429 with `Retry-After`; `USAGE_EXCEEDED` (no credits left): a 429, or a
      402 with `credits_status=402`.
    - Unkey down, slow (`timeout`) or limiting the workspace: a 503. A root key without the verify
      permission: UnkeyError, a 500.

    A route that needs other permissions or costs more credits has a strategy of its own, with
    another name and the same client: `UnkeyStrategy(verifier, name="unkey-admin",
    permissions="admin.*")`.

    :param unkey: the client, with a root key that may verify keys.
    :param verify: `verify(key)`: the user of a valid key (load it by `key.external_id`), or None
        to refuse it. Async, or a plain function, which runs in a thread.
    :param permissions: what the key must have, in Unkey's query syntax:
        `"documents.read AND users.view"`.
    :param cost: credits each verification spends (0 for none); a key without a credit limit
        spends nothing.
    :param ratelimits: names of the key's (or its identity's) rate limits to apply besides those
        it applies by itself.
    :param keyspaces: the keyspaces (APIs) the key must belong to; any by default.
    :param tags: tags for Unkey's analytics, `key=value`.
    :param header: the header the key comes in (`X-API-Key`), instead of `Authorization: Bearer`.
    :param credits_status: what a key out of credits gets: 429, or 402 (Payment Required).
    :param name: what `FastAPIAuth.authenticate` calls it by.
    :param metrics: where to count the verifications (`fastapi_alcazaba.auth.unkey`), by code;
        FastAPICache's by default.
    """

    def __init__(
        self,
        unkey: UnkeyClient,
        verify: Callable[[UnkeyKey], Any] | None = None,
        *,
        permissions: str | None = None,
        cost: int = 1,
        ratelimits: Sequence[str] = (),
        keyspaces: Sequence[str] = (),
        tags: Sequence[str] = (),
        header: str | None = None,
        credits_status: int = HTTP_429_TOO_MANY_REQUESTS,
        name: str = "unkey",
        metrics: Metrics | None = None,
    ) -> None:
        super().__init__(name)
        if permissions is not None and not 1 <= len(permissions) <= 1000:
            raise ValueError("permissions is a query of 1 to 1000 characters")
        if cost < 0:
            raise ValueError("cost can't be negative")
        if isinstance(ratelimits, str) or isinstance(keyspaces, str) or isinstance(tags, str):
            raise TypeError("ratelimits, keyspaces and tags are lists of names")
        if len(keyspaces) > 5 or len(tags) > 20 or len(ratelimits) > 50:
            raise ValueError("at most 5 keyspaces, 20 tags and 50 rate limits")
        if credits_status not in (HTTP_402_PAYMENT_REQUIRED, HTTP_429_TOO_MANY_REQUESTS):
            raise ValueError("credits_status is 429 (Too Many Requests) or 402 (Payment Required)")
        self.unkey = unkey
        self.verify = verify
        self.header = header
        self.credits_status = credits_status
        self.challenge = None if header is not None else "Bearer"
        self._metrics = metrics
        self._query: dict[str, Any] = {"credits": {"cost": cost}}
        if permissions is not None:
            self._query["permissions"] = permissions
        if ratelimits:
            self._query["ratelimits"] = [{"name": limit} for limit in ratelimits]
        if keyspaces:
            self._query["keyspaces"] = list(keyspaces)
        if tags:
            self._query["tags"] = list(tags)

    def openapi_scheme(self) -> HTTPBearer | APIKeyHeader:
        if self.header is not None:
            return APIKeyHeader(name=self.header, scheme_name=self.name, auto_error=False)
        return HTTPBearer(scheme_name=self.name, auto_error=False)

    def _key(self, connection: HTTPConnection) -> str | None:
        if self.header is not None:
            key = connection.headers.get(self.header)
            return None if key is None else key.strip()
        scheme, _, token = connection.headers.get("authorization", "").partition(" ")
        return token.strip() if scheme.lower() == "bearer" else None

    async def authenticate(self, connection: HTTPConnection) -> Any:
        key = self._key(connection)
        if key is None:
            return None
        if not _KEY.fullmatch(key):
            raise AuthenticationFailed("malformed key")  # not even sent to Unkey
        try:
            data, request_id = await self.unkey.call("keys.verifyKey", {"key": key, **self._query})
        except UnkeyUnavailable as exc:
            self._count("unavailable")
            logger.warning("Refused: Unkey is unavailable: %s", exc)
            self._refuse(connection, HTTP_503_SERVICE_UNAVAILABLE, "Authentication unavailable")
        code = data.get("code")
        self._count(code if isinstance(code, str) else "unknown")
        verified = _key_of(data, request_id)
        if data.get("valid") is True and code == "VALID":
            user = await call(self.verify, verified) if self.verify is not None else verified
            if user is None:
                raise AuthenticationFailed(f"key {verified.key_id} refused by verify")
            return user
        if code in _INVALID:
            raise AuthenticationFailed(f"key {code.lower()}")
        if code in ("INSUFFICIENT_PERMISSIONS", "FORBIDDEN"):
            self._refuse(connection, HTTP_403_FORBIDDEN, "Insufficient permissions")
        if code == "RATE_LIMITED":
            resets = [limit.reset for limit in verified.ratelimits if limit.exceeded]
            retry = {"Retry-After": str(max(1, math.ceil(max(resets))))} if resets else None
            self._refuse(connection, HTTP_429_TOO_MANY_REQUESTS, "Too Many Requests", retry)
        if code == "USAGE_EXCEEDED":
            self._refuse(connection, self.credits_status, "No credits left")
        # A code this version doesn't know: whatever it means, it isn't VALID.
        logger.warning("Refused: Unkey answered the code %r, which isn't VALID", code)
        raise AuthenticationFailed(f"key not valid ({code!r})")

    def _refuse(
        self,
        connection: HTTPConnection,
        status: int,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> NoReturn:
        if connection.scope["type"] == "websocket":
            code = (
                WS_1013_TRY_AGAIN_LATER
                if status == HTTP_503_SERVICE_UNAVAILABLE
                else WS_1008_POLICY_VIOLATION
            )
            raise WebSocketException(code, detail)
        raise HTTPException(status, detail, headers=headers)

    def _count(self, code: str) -> None:
        if self._metrics is not None:
            metrics = self._metrics
        else:
            from fastapi_alcazaba.response_cache.caches import FastAPICache

            metrics = FastAPICache.get_metrics()
        metrics.count("auth.unkey", strategy=self.name, code=code)


class UnkeyKeys:
    """Issues and revokes the keys of an Unkey API: for the page where your users make theirs.

    :param unkey: the client, with a root key that may create and delete keys of `api_id`.
    :param api_id: the Unkey API the keys belong to.
    :param prefix: what the keys start with (`sk_live`), up to 16 letters, digits and `_`.
    """

    def __init__(self, unkey: UnkeyClient, api_id: str, *, prefix: str | None = None) -> None:
        if not _API_ID.fullmatch(api_id):
            raise ValueError("api_id is 3 to 255 letters, digits and '_'")
        if prefix is not None and not _PREFIX.fullmatch(prefix):
            raise ValueError("a prefix is 1 to 16 letters, digits and '_'")
        self.unkey = unkey
        self.api_id = api_id
        self.prefix = prefix

    async def create(
        self,
        *,
        external_id: str | None = None,
        name: str | None = None,
        expires: datetime | None = None,
        credits_left: int | None = None,
        refill: CreditRefill | None = None,
        ratelimits: Sequence[KeyRatelimit] = (),
        permissions: Sequence[str] = (),
        roles: Sequence[str] = (),
        meta: Mapping[str, Any] | None = None,
    ) -> CreatedKey:
        """A new key, whose `key` to show its owner once. `external_id` ties it to its owner in
        your app (verifications give it back); `credits_left` are the uses it has (None for no
        limit), which `refill` gives back; `expires` is when it stops working."""
        body: dict[str, Any] = {"apiId": self.api_id}
        if self.prefix is not None:
            body["prefix"] = self.prefix
        if external_id is not None:
            if not _EXTERNAL_ID.fullmatch(external_id):
                raise ValueError("external_id is 1 to 255 letters, digits and '_.-'")
            body["externalId"] = external_id
        if name is not None:
            body["name"] = name
        if expires is not None:
            if expires.tzinfo is None:
                raise ValueError("expires is an aware datetime (with its time zone)")
            body["expires"] = int(expires.timestamp() * 1000)
        if refill is not None and credits_left is None:
            raise ValueError("a refill gives back credits: give the key some (credits_left)")
        if credits_left is not None:
            if credits_left < 0:
                raise ValueError("credits_left can't be negative")
            body["credits"] = {"remaining": credits_left}
            if refill is not None:
                body["credits"]["refill"] = {"interval": refill.interval, "amount": refill.amount}
                if refill.day is not None:
                    body["credits"]["refill"]["refillDay"] = refill.day
        if ratelimits:
            body["ratelimits"] = [
                {
                    "name": limit.name,
                    "limit": limit.limit,
                    "duration": int(limit.duration.total_seconds() * 1000),
                    "autoApply": limit.auto_apply,
                }
                for limit in ratelimits
            ]
        if permissions:
            body["permissions"] = list(permissions)
        if roles:
            body["roles"] = list(roles)
        if meta is not None:
            body["meta"] = dict(meta)
        data, request_id = await self.unkey.call("keys.createKey", body)
        key, key_id = data.get("key"), data.get("keyId")
        if not isinstance(key, str) or not isinstance(key_id, str):
            raise UnkeyError(200, "keys.createKey answered without a key", request_id)
        return CreatedKey(key=SecretStr(key), key_id=key_id)

    async def delete(self, key_id: str, *, permanent: bool = False) -> None:
        """Delete a key: its verifications fail within 30 seconds. `permanent` erases it whole
        (its hash and metadata too); otherwise Unkey can still bring it back."""
        if not _KEY_ID.fullmatch(key_id):
            raise ValueError("key_id is Unkey's ID of the key (keyId), not the key")
        await self.unkey.call("keys.deleteKey", {"keyId": key_id, "permanent": permanent})
