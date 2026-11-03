"""JWTs in `Authorization: Bearer`, verified with PyJWT:
pip install "fastapi-alcazaba[auth-strategy-jwt]".

    jwt = auth.use(JWTStrategy(key=settings.jwt_secret, verify=load_user))

    @app.post("/login")
    async def login(...) -> dict[str, str]:
        return {"access_token": jwt.issue(user.id), "token_type": "bearer"}

Keys of any type rotate: the first of `keys` signs, the others verify until their `until`:

    retires = datetime(2026, 12, 1, tzinfo=UTC)
    JWTStrategy(keys=[
        JWTKey(settings.new_private_key, algorithm="ES256"),
        JWTKey(settings.old_private_key, algorithm="RS256", until=retires),
    ])

Tokens of an identity provider (Auth0, Keycloak, Cognito...) are verified with its public keys,
fetched from its JWKS: `JWTStrategy(jwks_url=..., audience=..., issuer=...)`.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import warnings
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from fastapi import HTTPException, WebSocketException
from fastapi.security import HTTPBearer
from starlette.requests import HTTPConnection
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE, WS_1013_TRY_AGAIN_LATER

from fastapi_alcazaba.auth.strategies.base import AuthenticationFailed, Strategy, _secure, call
from fastapi_alcazaba.auth.tokens import TokenBlocklist
from fastapi_alcazaba.core.integrity import Secrets, reveal, secret_list
from fastapi_alcazaba.security._cookies import cookie_values

try:
    import jwt
except ImportError as exc:  # pragma: no cover  # the extra isn't installed
    raise ImportError(
        'JWTStrategy needs PyJWT: pip install "fastapi-alcazaba[auth-strategy-jwt]"'
    ) from exc

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: The longest token read from a request.
MAX_TOKEN = 8_192
#: Seconds between two fetches of the JWKS, at least: a token with a made-up `kid` doesn't make
#: the app fetch it on every request, and a provider that's down isn't called on every request.
JWKS_REFETCH = 30
#: The largest JWKS read, in bytes.
MAX_JWKS = 256 * 1024
#: The algorithms of a JWKS by default (OIDCStrategy's).
JWKS_ALGORITHMS = ("RS256", "ES256", "PS256", "EdDSA")
#: What a secret's `kid` is the HMAC of: it names the secret without revealing anything of it.
_KID_LABEL = b"fastapi-alcazaba jwt kid"
#: The members of a public JWK its RFC 7638 thumbprint covers, by key type.
_THUMBPRINT = {"RSA": ("e", "kty", "n"), "EC": ("crv", "kty", "x", "y"), "OKP": ("crv", "kty", "x")}
_ALGORITHMS = {
    name: algorithm
    for name, algorithm in jwt.algorithms.get_default_algorithms().items()
    if name != "none"
}


class JWKSUnavailable(Exception):
    """The provider's keys can't be had: its JWKS didn't answer (or not with keys) and those
    fetched before are too old. `JWTStrategy` refuses the request with a 503."""


class JWTKey:
    """A key of `JWTStrategy`, pinned to one algorithm: it signs and verifies with that one only,
    so a public key can never be taken for an HMAC secret.

    :param key: an HMAC secret of at least 32 bytes (HS256, HS384, HS512); a private key (PEM, or
        a key object), which signs, and whose public part verifies; or a public key, which only
        verifies.
    :param algorithm: HS256, RS256, PS256, ES256, EdDSA...
    :param kid: the key's id, in the header of the tokens it signs. By default the RFC 7638
        thumbprint of a public key, and an HMAC of a constant for a secret, which reveals nothing
        of it.
    :param until: when it retires (an aware datetime): past it, the tokens it signed are refused.
        Put the rotation's time plus the longest lifetime of those tokens.
    """

    def __init__(
        self,
        key: Any,
        *,
        algorithm: str,
        kid: str | None = None,
        until: datetime | None = None,
    ) -> None:
        if algorithm not in _ALGORITHMS:
            raise ValueError(f"unknown algorithm {algorithm!r}; 'none' is never one")
        if isinstance(key, list | tuple):
            raise TypeError("one key per JWTKey; keys=[...] takes several")
        if kid is not None and (not isinstance(kid, str) or not kid):
            raise ValueError("kid is a non-empty string, or None to derive it")
        if until is not None and until.utcoffset() is None:
            raise ValueError("until is an aware datetime: datetime(..., tzinfo=UTC)")
        self.algorithm = algorithm
        self.until = until
        #: Whether `kid` was given: a token with an unknown `kid` is tried with the keys whose
        #: `kid` wasn't.
        self.kid_given = kid is not None
        #: The algorithms its tokens may have: `algorithm`, and those of `algorithms=` for a key
        #: made from `key=`.
        self.algorithms: tuple[str, ...] = (algorithm,)
        handler = _ALGORITHMS[algorithm]
        if algorithm.startswith("HS"):
            secret = secret_list(key)[0]
            try:
                handler.prepare_key(secret)
            except jwt.InvalidKeyError:
                raise ValueError(
                    f"a public key isn't an HMAC secret: it can't be {algorithm}'s"
                ) from None
            self._signing: Any = secret
            self._verifying: Any = secret
            derived = _b64(hmac.new(secret, _KID_LABEL, hashlib.sha256).digest())[:22]
        else:
            try:
                prepared = handler.prepare_key(reveal(key))
            except (jwt.InvalidKeyError, ValueError, TypeError) as exc:
                raise ValueError(f"not a key for {algorithm}: {exc}") from None
            if hasattr(prepared, "public_key"):  # a private key
                self._signing, self._verifying = prepared, prepared.public_key()
            else:
                self._signing, self._verifying = None, prepared
            derived = _thumbprint(self.public_jwk())
        self.kid: str = kid if kid is not None else derived

    def __repr__(self) -> str:
        kid = getattr(self, "kid", None)  # None while __init__ refuses the key
        return f"JWTKey(kid={kid!r}, algorithm={self.algorithm!r}, until={self.until!r})"

    @property
    def can_sign(self) -> bool:
        """Whether it signs: an HMAC secret, or a private key."""
        return self._signing is not None

    @property
    def symmetric(self) -> bool:
        """Whether it's an HMAC secret."""
        return self.algorithm.startswith("HS")

    def retired(self, now: float | None = None) -> bool:
        """Whether its `until` has passed."""
        if self.until is None:
            return False
        return (time.time() if now is None else now) >= self.until.timestamp()

    def encode(self, payload: dict[str, Any]) -> str:
        """`payload` signed, with this key's `kid` in the header."""
        if self._signing is None:
            raise RuntimeError("issue() needs a key that signs (a private key, or an HMAC secret)")
        return jwt.encode(
            payload, self._signing, algorithm=self.algorithm, headers={"kid": self.kid}
        )

    def decode(self, token: str, algorithm: str, **options: Any) -> dict[str, Any]:
        """The claims of `token`, verified with this key and `algorithm`, one of its own;
        `options` are PyJWT's (`audience`, `issuer`, `leeway`, `options`)."""
        if algorithm not in self.algorithms:
            raise jwt.InvalidAlgorithmError(f"{self.kid} isn't for {algorithm}")
        claims: dict[str, Any] = jwt.decode(
            token, self._verifying, algorithms=[algorithm], **options
        )
        return claims

    def public_jwk(self) -> dict[str, Any]:
        """Its public key as a JWK (RFC 7517), without `kid` nor `alg`; ValueError for a secret."""
        if self.symmetric:
            raise ValueError("an HMAC secret has no public key")
        jwk: dict[str, Any] = _ALGORITHMS[self.algorithm].to_jwk(self._verifying, as_dict=True)
        return jwk


class _FetchFailed(Exception):
    """The JWKS wasn't fetched: no answer, not a 200, not a JWKS with a usable key."""


class _RemoteKeys:
    """The keys of a JWKS, fetched with httpx2 through ResilientTransport (a deadline and a
    circuit breaker), kept for `lifespan` seconds, and `stale` more while it can't be fetched."""

    def __init__(
        self,
        url: str,
        *,
        algorithms: Sequence[str],
        lifespan: float,
        stale: float,
        timeout: float,
        client: Any,
        name: str,
    ) -> None:
        try:
            import httpx2 as httpx
        except ImportError:  # pragma: no cover  # httpx under its old name
            try:
                import httpx  # type: ignore[no-redef,import-not-found]
            except ImportError as exc:
                raise ImportError(
                    'jwks_url needs httpx2: pip install "fastapi-alcazaba[auth-strategy-jwt]"'
                ) from exc
        from fastapi_alcazaba.outbound.transports import ResilientTransport

        self.url = url
        self.algorithms = list(algorithms)
        self.lifespan = lifespan
        self.stale = stale
        self.timeout = timeout
        self.name = name
        self._own_client = client is None
        self.client = (
            client
            if client is not None
            else httpx.AsyncClient(
                transport=ResilientTransport(timeout=timeout, max_response_size=MAX_JWKS)
            )
        )
        self._keys: dict[str | None, list[dict[str, Any]]] = {}
        self._parsed: dict[tuple[str | None, int, str], jwt.PyJWK] = {}
        self._fetched = -float("inf")
        self._attempted = -float("inf")
        self._lock = asyncio.Lock()

    async def key(self, kid: str | None, algorithm: str) -> jwt.PyJWK:
        """The provider's key `kid` for `algorithm`: the JWKS is fetched again when it's older
        than `lifespan`, or hasn't the key (a rotation), at most every JWKS_REFETCH seconds."""
        if algorithm not in self.algorithms:
            raise AuthenticationFailed(f"the token's algorithm isn't accepted: {algorithm!r}")
        if time.monotonic() - self._fetched > self.lifespan or kid not in self._keys:
            await self._refresh()
        if time.monotonic() - self._fetched > self.lifespan + self.stale:
            raise JWKSUnavailable(f"{self.name}: no keys of {self.url} recent enough")
        found = self._keys.get(kid)
        if found is None and kid is None and len(self._keys) == 1:
            found = next(iter(self._keys.values()))  # a provider with one key may not name it
        if found is None:
            raise AuthenticationFailed(f"the token's key isn't the provider's: {kid!r}")
        for index, data in enumerate(found):
            if data.get("alg", algorithm) != algorithm:
                continue  # pinned to another algorithm
            parsed = self._parsed.get((kid, index, algorithm))
            if parsed is None:
                try:
                    parsed = jwt.PyJWK(data, algorithm=algorithm)
                except jwt.PyJWTError:
                    continue  # a key of another type (an EC key for RS256)
                self._parsed[kid, index, algorithm] = parsed
            return parsed
        raise AuthenticationFailed(f"the provider's key {kid!r} isn't for {algorithm}")

    async def _refresh(self) -> None:
        # One fetch at a time: those that waited for it use what it got.
        async with self._lock:
            if time.monotonic() - self._attempted < JWKS_REFETCH:
                return
            self._attempted = time.monotonic()
            try:
                keys = await self._fetch()
            except _FetchFailed as exc:
                logger.warning("%s: the JWKS wasn't fetched: %s", self.name, exc)
                _count(self.name, "failed")
                return
            self._keys = keys
            self._parsed = {}
            self._fetched = self._attempted
            _count(self.name, "fetched")

    async def _fetch(self) -> dict[str | None, list[dict[str, Any]]]:
        try:
            response = await self.client.get(
                self.url, headers={"Accept": "application/json"}, timeout=self.timeout
            )
        # httpx's errors (refused, timed out, too large, its circuit breaker open), or any of a
        # client given: either way there are no keys.
        except Exception as exc:
            raise _FetchFailed(f"{self.url} didn't answer: {exc!r}") from exc
        if response.status_code != 200:
            raise _FetchFailed(f"{self.url} answered {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise _FetchFailed(f"{self.url} didn't answer JSON") from None
        keys = _usable_keys(data, self.algorithms)
        if not keys:
            raise _FetchFailed(f"{self.url} has no signing keys for {self.algorithms}")
        return keys

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()


class JWTStrategy(Strategy):
    """Authenticates by a JWT in `Authorization: Bearer <token>` (or a cookie), verified with
    the app's keys or the provider's JWKS. The signature, `exp`, `nbf` and the claims of
    `required` are always checked, `aud` and `iss` when given; a token with an `aud` is refused
    when no `audience` is, as it's meant for some other service. `alg: none` is never accepted,
    and each key verifies with its own algorithm only.

    Give one of `keys`, `key` and `jwks_url`.

    :param key: what verifies the tokens: a secret of at least 32 bytes for HS256, HS384 and
        HS512 (a list rotates them: the first one signs, all of them verify), or a public key
        (PEM) for RS256, ES256, EdDSA... `keys` does this for keys of any type.
    :param keys: `JWTKey`s, each with its algorithm: the first one signs (when it can) and
        doesn't retire, all of them verify, until their `until`. HMAC and asymmetric keys may be
        mixed (from HS256 to RS256). A token is verified with the key its `kid` names; one
        without a `kid`, or with one no key has, with the keys whose `kid` wasn't given (tokens
        issued before 0.8 have none).
    :param jwks_url: the provider's JWKS (HTTPS) instead of keys: its keys are fetched, by the
        `kid` of each token, and kept for `jwks_lifespan` seconds; a `kid` it doesn't have makes
        it fetched again, at most every 30 seconds. Needs `audience` and `issuer`: the provider
        signs the tokens of every app it serves with the same keys.
    :param algorithms: those accepted. With `key`, the first one signs (HS256 by default); with
        `jwks_url`, asymmetric ones (RS256, ES256, PS256 and EdDSA by default). Not with `keys`.
    :param audience: the `aud` the tokens must have.
    :param issuer: the `iss` the tokens must have.
    :param leeway: seconds of tolerance on `exp` and `nbf`, for clocks out of step; the
        blocklist's `leeway` must be at least as long.
    :param required: claims a token must have.
    :param verify: `verify(claims)`: the user, or None to refuse the token. Async, or a plain
        function, which runs in a thread. By default the user is the claims themselves.
    :param blocklist: refuses the tokens revoked there (by `jti`, by `sid` for one login, and by
        `sub` and `iat` for "log out everywhere"); tokens then need a `jti` and an `iat`.
    :param cookie: also reads the token from this cookie, for browsers.
    :param signing_key: deprecated: `keys=[JWTKey(private_key, algorithm=...)]`. What `issue`
        signs with, for `key` with asymmetric algorithms (the private key).
    :param jwks_lifespan: seconds the provider's keys are kept before they're fetched again.
    :param jwks_stale: seconds more they're used while the JWKS can't be fetched; then the
        requests get a 503 until it can.
    :param jwks_timeout: seconds for the whole fetch of the JWKS.
    :param jwks_client: the httpx2 client to fetch the JWKS with; one of its own by default, with
        a deadline and a circuit breaker (`ResilientTransport`), closed by `aclose()`.
    :param scope_claims: the claims whose scopes the token grants, for the routes that need some
        (`Security(..., scopes=[...])`): RFC 9068's `scope`, a string of them separated by
        spaces, and `scp`, a list (Entra ID, Okta); `permissions` for Auth0's.
    :param name: what `FastAPIAuth.authenticate` calls it by.
    """

    challenge = "Bearer"

    def __init__(
        self,
        key: Secrets | None = None,
        *,
        keys: Sequence[JWTKey] | None = None,
        jwks_url: str | None = None,
        algorithms: Sequence[str] | None = None,
        audience: str | None = None,
        issuer: str | None = None,
        leeway: float = 0,
        required: Sequence[str] = ("exp", "sub"),
        verify: Callable[[dict[str, Any]], Any] | None = None,
        blocklist: TokenBlocklist | None = None,
        cookie: str | None = None,
        signing_key: Secrets | None = None,
        jwks_lifespan: float = 300,
        jwks_stale: float = 3600,
        jwks_timeout: float = 3.0,
        jwks_client: Any = None,
        scope_claims: Sequence[str] = ("scope", "scp"),
        name: str = "jwt",
    ) -> None:
        super().__init__(name)
        if [key is not None, keys is not None, jwks_url is not None].count(True) != 1:
            raise ValueError("give keys, key or jwks_url, one of them")
        if blocklist is not None and blocklist.leeway < leeway:
            # Its marks would expire while the strategy still accepts the token.
            raise ValueError(
                f"the blocklist keeps revocations {blocklist.leeway} s past exp, less than the "
                f"leeway ({leeway} s): give it TokenBlocklist(leeway={leeway}) at least"
            )
        if signing_key is not None:
            if keys is not None:
                raise ValueError("with keys, the first one signs: no signing_key")
            warnings.warn(
                "signing_key is deprecated: give keys=[JWTKey(private_key, algorithm=...)]",
                DeprecationWarning,
                stacklevel=2,
            )
        self._keys: list[JWTKey] = []
        self._remote: _RemoteKeys | None = None
        if keys is not None:
            if algorithms is not None:
                raise ValueError("with keys, each key has its algorithm: no algorithms")
            self._keys = _check_keys(keys)
            accepted = [one.algorithm for one in self._keys]
        else:
            default = JWKS_ALGORITHMS if jwks_url is not None else ("HS256",)
            accepted = _check_algorithms(algorithms if algorithms is not None else default)
            symmetric = accepted[0].startswith("HS")
            if key is not None:
                self._keys = [_legacy(one, accepted) for one in _key_list(key, symmetric)]
            else:
                if symmetric:
                    raise ValueError("a JWKS has public keys: asymmetric algorithms (RS256...)")
                self._remote = _remote_keys(
                    str(jwks_url),
                    accepted,
                    audience,
                    issuer,
                    name,
                    lifespan=jwks_lifespan,
                    stale=jwks_stale,
                    timeout=jwks_timeout,
                    client=jwks_client,
                )
        self._signer: JWTKey | None = self._keys[0] if self._keys else None
        if signing_key is not None:
            one = secret_list(signing_key)[0] if accepted[0].startswith("HS") else signing_key
            self._signer = _legacy(_single(one), accepted)
        self.algorithms = list(dict.fromkeys(accepted))
        self.audience = audience
        self.issuer = issuer
        self.leeway = leeway
        self.required = list(dict.fromkeys([*required, *(("jti", "iat") if blocklist else ())]))
        self.verify = verify
        self.blocklist = blocklist
        self.cookie = cookie
        self.scope_claims = list(scope_claims)

    @property
    def can_issue(self) -> bool:
        """Whether `issue` can sign: an HMAC secret, or a private key, first."""
        return self._signer is not None and self._signer.can_sign

    @property
    def keys(self) -> list[JWTKey]:
        """The keys, the one that signs first; none with a JWKS."""
        return list(self._keys)

    def openapi_scheme(self) -> HTTPBearer:
        return HTTPBearer(bearerFormat="JWT", scheme_name=self.name, auto_error=False)

    async def authenticate(self, connection: HTTPConnection) -> Any:
        token = self._token(connection)
        if token is None:
            return None
        try:
            claims = await self.decode(token)
        except JWKSUnavailable as exc:
            logger.warning("Refused: %s", exc)
            if connection.scope["type"] == "websocket":
                raise WebSocketException(
                    WS_1013_TRY_AGAIN_LATER, "Authentication unavailable"
                ) from None
            raise HTTPException(
                HTTP_503_SERVICE_UNAVAILABLE,
                "Authentication unavailable",
                headers={"Retry-After": str(JWKS_REFETCH)},
            ) from None
        if self.blocklist is not None and await self.blocklist.is_revoked(
            str(claims["jti"]),
            subject=_subject(claims),
            issued_at=claims["iat"],
            session=_session(claims),
        ):
            raise AuthenticationFailed("the token was revoked")
        self.grant(connection, _scopes(claims, self.scope_claims))
        if self.verify is None:
            return claims
        user = await call(self.verify, claims)
        if user is None:
            raise AuthenticationFailed("verify() refused the token")
        return user

    async def decode(self, token: str) -> dict[str, Any]:
        """The claims of `token`, verified; raises AuthenticationFailed when it isn't valid, and
        JWKSUnavailable when the provider's keys can't be had."""
        if len(token) > MAX_TOKEN:
            raise AuthenticationFailed("the token is too long")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthenticationFailed(f"invalid token ({type(exc).__name__})") from None
        algorithm, kid = header.get("alg"), header.get("kid")
        if not isinstance(algorithm, str) or not (kid is None or isinstance(kid, str)):
            raise AuthenticationFailed("invalid token (its alg or kid isn't a string)")
        try:
            if self._remote is not None:
                found = await self._remote.key(kid, algorithm)
                claims: dict[str, Any] = jwt.decode(
                    token, found, algorithms=[algorithm], **self._options()
                )
                return claims
            candidates = self._candidates(kid, algorithm)
            for key in candidates[:-1]:
                try:
                    return key.decode(token, algorithm, **self._options())
                except jwt.InvalidSignatureError:
                    continue  # signed with another of the keys, maybe
            return candidates[-1].decode(token, algorithm, **self._options())
        except jwt.PyJWTError as exc:
            raise AuthenticationFailed(f"invalid token ({type(exc).__name__})") from None

    def issue(
        self,
        subject: str,
        *,
        expires_in: int = 900,
        **claims: Any,
    ) -> str:
        """A token for `subject`, valid for `expires_in` seconds, with `iat`, `exp`, a random
        `jti`, `iss` and `aud` when they're set, and `claims`; signed with the first key, whose
        `kid` goes in its header."""
        if self._signer is None:
            raise RuntimeError("issue() needs a key that signs (a private key, or an HMAC secret)")
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": subject,
            "iat": now,
            "exp": now + expires_in,
            "jti": secrets.token_urlsafe(16),
        }
        if self.issuer is not None:
            payload["iss"] = self.issuer
        if self.audience is not None:
            payload["aud"] = self.audience
        payload.update(claims)
        return self._signer.encode(payload)

    def jwks(self) -> dict[str, list[dict[str, Any]]]:
        """The public keys as a JWKS, for those who verify the app's tokens (serve it at
        `/.well-known/jwks.json`): every asymmetric key that hasn't retired, with its `kid`,
        `alg` and `use`. Never an HMAC secret."""
        now = time.time()
        return {
            "keys": [
                {**key.public_jwk(), "kid": key.kid, "alg": key.algorithm, "use": "sig"}
                for key in self._keys
                if not key.symmetric and not key.retired(now)
            ]
        }

    async def aclose(self) -> None:
        """Closes the client it made to fetch the JWKS (not one it was given): in the lifespan,
        at shutdown."""
        if self._remote is not None:
            await self._remote.aclose()

    def _candidates(self, kid: str | None, algorithm: str) -> list[JWTKey]:
        """The keys that may have signed a token with this `kid` and `alg`: the one it names, or
        those whose `kid` wasn't given; refused when there are none."""
        named = [key for key in self._keys if key.kid == kid]
        now = time.time()
        if named and named[0].retired(now):
            raise AuthenticationFailed(f"invalid token: its key {kid!r} retired")
        keys = named or [key for key in self._keys if not key.kid_given]
        candidates = [key for key in keys if algorithm in key.algorithms and not key.retired(now)]
        if not candidates:
            raise AuthenticationFailed(
                f"invalid token: no key for kid {kid!r} and alg {algorithm!r}"
            )
        return candidates

    def _options(self) -> dict[str, Any]:
        """PyJWT's options for every token."""
        return {
            "audience": self.audience,
            "issuer": self.issuer,
            "leeway": self.leeway,
            "options": {"require": self.required},
        }

    def _token(self, connection: HTTPConnection) -> str | None:
        scheme, _, token = connection.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
        if self.cookie is not None:
            values = cookie_values(connection.scope, self.cookie)
            if values:
                return values[0]
        return None


def _check_keys(keys: Sequence[JWTKey]) -> list[JWTKey]:
    checked = list(keys)
    if not checked or not all(isinstance(key, JWTKey) for key in checked):
        raise ValueError("keys is a list of JWTKey, the one that signs first")
    if checked[0].until is not None:
        raise ValueError("the first key signs: it doesn't retire (no until)")
    kids = [key.kid for key in checked]
    if repeated := sorted({kid for kid in kids if kids.count(kid) > 1}):
        raise ValueError(f"two keys have one kid: {repeated}")
    return checked


def _check_algorithms(algorithms: Sequence[str]) -> list[str]:
    accepted = list(algorithms)
    if not accepted or any(algorithm.lower() == "none" for algorithm in accepted):
        raise ValueError("give the algorithms to accept; 'none' is never one of them")
    if unknown := [algorithm for algorithm in accepted if algorithm not in _ALGORITHMS]:
        raise ValueError(f"unknown algorithms: {unknown}")
    symmetric = [algorithm for algorithm in accepted if algorithm.startswith("HS")]
    if symmetric and len(symmetric) != len(accepted):
        # A public key accepted as an HMAC secret would let anyone sign tokens.
        raise ValueError("don't mix HMAC algorithms (HS256...) with asymmetric ones")
    return accepted


def _remote_keys(
    url: str,
    algorithms: list[str],
    audience: str | None,
    issuer: str | None,
    name: str,
    *,
    lifespan: float,
    stale: float,
    timeout: float,
    client: Any,
) -> _RemoteKeys:
    if audience is None or issuer is None:
        raise ValueError(
            "with jwks_url, give the audience and the issuer: the provider signs the tokens "
            "of every app it serves with the same keys"
        )
    if not _secure(url):
        raise ValueError(f"jwks_url is HTTPS (or http://localhost): {url}")
    if lifespan <= 0 or timeout <= 0:
        raise ValueError("jwks_lifespan and jwks_timeout must be positive")
    if stale < 0:
        raise ValueError("jwks_stale can't be negative")
    return _RemoteKeys(
        url,
        algorithms=algorithms,
        lifespan=lifespan,
        stale=stale,
        timeout=timeout,
        client=client,
        name=name,
    )


def _key_list(key: Secrets, symmetric: bool) -> list[Any]:
    """The keys of `key=`: HMAC secrets (a list rotates them), or one public key."""
    if symmetric:
        return list(secret_list(key))
    return [_single(key)]


def _single(key: Any) -> Any:
    if isinstance(key, list | tuple):
        raise ValueError("only HMAC secrets rotate with key=; give keys=[JWTKey(...), ...]")
    return key


def _legacy(key: Any, algorithms: list[str]) -> JWTKey:
    """A key of `key=` or `signing_key=`: every algorithm of `algorithms` verifies with it, and
    its `kid` counts as not given."""
    made = JWTKey(key, algorithm=algorithms[0])
    made.kid_given = False
    made.algorithms = tuple(algorithms)
    return made


def _usable_keys(data: Any, algorithms: Sequence[str]) -> dict[str | None, list[dict[str, Any]]]:
    """The signing keys of a JWKS, by `kid`: not for encryption (`use: enc`), not HMAC secrets
    (`oct`: a provider's keys are public), of a type PyJWT knows and with an `alg` accepted."""
    keys: dict[str | None, list[dict[str, Any]]] = {}
    entries = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return keys
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("use", "sig") != "sig":
            continue
        kid, alg = entry.get("kid"), entry.get("alg")
        if entry.get("kty") == "oct" or not (kid is None or isinstance(kid, str)):
            continue
        if alg is not None and alg not in algorithms:
            continue
        try:
            jwt.PyJWK(entry)
        except jwt.PyJWTError:
            continue
        keys.setdefault(kid, []).append(entry)
    return keys


def _thumbprint(jwk: dict[str, Any]) -> str:
    """The RFC 7638 thumbprint of a public JWK (SHA-256), base64url."""
    members = {name: jwk[name] for name in _THUMBPRINT[jwk["kty"]]}
    canonical = json.dumps(members, separators=(",", ":"), sort_keys=True).encode()
    return _b64(hashlib.sha256(canonical).digest())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _count(strategy: str, outcome: str) -> None:
    from fastapi_alcazaba.response_cache.caches import FastAPICache

    FastAPICache.get_metrics().count("auth.jwks", strategy=strategy, outcome=outcome)


def _scopes(claims: dict[str, Any], names: Sequence[str]) -> list[str]:
    """The scopes of `claims`' `names`: strings of them separated by spaces, or lists."""
    scopes: list[str] = []
    for name in names:
        value = claims.get(name)
        if isinstance(value, str):
            scopes += value.split()
        elif isinstance(value, list):
            scopes += value  # grant() refuses what isn't a scope token
        elif value is not None:
            raise AuthenticationFailed(f"the token's {name} isn't a string nor a list")
    return scopes


def _subject(claims: dict[str, Any]) -> str | None:
    subject = claims.get("sub")
    return str(subject) if subject is not None else None


def _session(claims: dict[str, Any]) -> str | None:
    """The token's session id (`sid`, see `RefreshTokens`), revoked with its login."""
    session = claims.get("sid")
    return str(session) if session is not None else None
