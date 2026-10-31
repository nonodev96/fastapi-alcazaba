"""JWTs in `Authorization: Bearer`, verified with PyJWT:
pip install "fastapi-alcazaba[auth-strategy-jwt]".

    jwt = auth.use(JWTStrategy(key=settings.jwt_secret, verify=load_user))

    @app.post("/login")
    async def login(...) -> dict[str, str]:
        return {"access_token": jwt.issue(user.id), "token_type": "bearer"}

Tokens of an identity provider (Auth0, Keycloak, Cognito...) are verified with its public keys,
fetched from its JWKS: `JWTStrategy(jwks_url=..., algorithms=["RS256"], audience=..., issuer=...)`.
"""

import secrets
import time
from collections.abc import Callable, Sequence
from typing import Any

from fastapi.security import HTTPBearer
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection

from fastapi_alcazaba.auth.strategies.base import AuthenticationFailed, Strategy, call
from fastapi_alcazaba.auth.tokens import TokenBlocklist
from fastapi_alcazaba.core.integrity import Secrets, secret_list
from fastapi_alcazaba.security._cookies import cookie_values

try:
    import jwt
except ImportError as exc:  # pragma: no cover  # the extra isn't installed
    raise ImportError(
        'JWTStrategy needs PyJWT: pip install "fastapi-alcazaba[auth-strategy-jwt]"'
    ) from exc

#: The longest token read from a request.
MAX_TOKEN = 8_192


class JWTStrategy(Strategy):
    """Authenticates by a JWT in `Authorization: Bearer <token>` (or a cookie), verified with
    `key` or the provider's JWKS. The signature, `exp`, `nbf` and the claims of `required` are
    always checked, `aud` and `iss` when given; a token with an `aud` is refused when no
    `audience` is, as it's meant for some other service. `alg: none` is never accepted.

    :param key: what verifies the tokens: a secret of at least 32 bytes for HS256, HS384 and
        HS512 (a list rotates them: the first one signs, all of them verify), or a public key
        (PEM) for RS256, ES256, EdDSA...
    :param jwks_url: the provider's JWKS instead of `key`: its keys are fetched, by the `kid` of
        each token, and kept for `jwks_lifespan` seconds. Needs `audience` and `issuer`: the
        provider signs the tokens of every app it serves with the same keys.
    :param algorithms: those accepted; the first one signs what `issue` makes.
    :param audience: the `aud` the tokens must have.
    :param issuer: the `iss` the tokens must have.
    :param leeway: seconds of tolerance on `exp` and `nbf`, for clocks out of step; the
        blocklist's `leeway` must be at least as long.
    :param required: claims a token must have.
    :param verify: `verify(claims)`: the user, or None to refuse the token. Async, or a plain
        function, which runs in a thread. By default the user is the claims themselves.
    :param blocklist: refuses the tokens revoked there (by `jti`, and by `sub` and `iat` for
        "log out everywhere"); tokens then need a `jti` and an `iat`.
    :param cookie: also reads the token from this cookie, for browsers.
    :param signing_key: what `issue` signs with, for asymmetric algorithms (the private key); by
        default the first `key`, for HMAC ones.
    :param name: what `FastAPIAuth.authenticate` calls it by.
    """

    challenge = "Bearer"

    def __init__(
        self,
        key: Secrets | None = None,
        *,
        jwks_url: str | None = None,
        algorithms: Sequence[str] = ("HS256",),
        audience: str | None = None,
        issuer: str | None = None,
        leeway: float = 0,
        required: Sequence[str] = ("exp", "sub"),
        verify: Callable[[dict[str, Any]], Any] | None = None,
        blocklist: TokenBlocklist | None = None,
        cookie: str | None = None,
        signing_key: Secrets | None = None,
        jwks_lifespan: float = 300,
        name: str = "jwt",
    ) -> None:
        super().__init__(name)
        if (key is None) == (jwks_url is None):
            raise ValueError("give key or jwks_url, one of them")
        if jwks_url is not None and (audience is None or issuer is None):
            raise ValueError(
                "with jwks_url, give the audience and the issuer: the provider signs the tokens "
                "of every app it serves with the same keys"
            )
        if not algorithms or any(algorithm.lower() == "none" for algorithm in algorithms):
            raise ValueError("give the algorithms to accept; 'none' is never one of them")
        hmac = [algorithm for algorithm in algorithms if algorithm.startswith("HS")]
        if hmac and len(hmac) != len(algorithms):
            # A public key accepted as an HMAC secret would let anyone sign tokens.
            raise ValueError("don't mix HMAC algorithms (HS256...) with asymmetric ones")
        if blocklist is not None and blocklist.leeway < leeway:
            # Its marks would expire while the strategy still accepts the token.
            raise ValueError(
                f"the blocklist keeps revocations {blocklist.leeway} s past exp, less than the "
                f"leeway ({leeway} s): give it TokenBlocklist(leeway={leeway}) at least"
            )
        self.algorithms = list(algorithms)
        self.audience = audience
        self.issuer = issuer
        self.leeway = leeway
        self.required = list(dict.fromkeys([*required, *(("jti", "iat") if blocklist else ())]))
        self.verify = verify
        self.blocklist = blocklist
        self.cookie = cookie
        self._keys: list[Any] = []
        if key is not None:
            self._keys = secret_list(key) if hmac else [_key(key)]
        self._jwks = jwt.PyJWKClient(jwks_url, lifespan=jwks_lifespan) if jwks_url else None
        if signing_key is not None:
            self._signing_key: Any = secret_list(signing_key)[0] if hmac else _key(signing_key)
        else:
            self._signing_key = self._keys[0] if hmac and self._keys else None

    def openapi_scheme(self) -> HTTPBearer:
        return HTTPBearer(bearerFormat="JWT", scheme_name=self.name, auto_error=False)

    async def authenticate(self, connection: HTTPConnection) -> Any:
        token = self._token(connection)
        if token is None:
            return None
        claims = await self.decode(token)
        if self.blocklist is not None and await self.blocklist.is_revoked(
            str(claims["jti"]), subject=_subject(claims), issued_at=claims["iat"]
        ):
            raise AuthenticationFailed("the token was revoked")
        if self.verify is None:
            return claims
        user = await call(self.verify, claims)
        if user is None:
            raise AuthenticationFailed("verify() refused the token")
        return user

    async def decode(self, token: str) -> dict[str, Any]:
        """The claims of `token`, verified; raises AuthenticationFailed when it isn't valid."""
        if len(token) > MAX_TOKEN:
            raise AuthenticationFailed("the token is too long")
        try:
            if self._jwks is not None:
                # PyJWKClient fetches with urllib, which blocks: in a thread.
                found = await run_in_threadpool(self._jwks.get_signing_key_from_jwt, token)
                return self._decode(token, found.key)
            for key in self._keys[:-1]:
                try:
                    return self._decode(token, key)
                except jwt.InvalidSignatureError:
                    continue  # signed with another of the keys, maybe
            return self._decode(token, self._keys[-1])
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
        `jti`, `iss` and `aud` when they're set, and `claims`."""
        if self._signing_key is None:
            raise RuntimeError("issue() needs a signing_key (a private key, or an HMAC secret)")
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
        return jwt.encode(payload, self._signing_key, algorithm=self.algorithms[0])

    def _decode(self, token: str, key: Any) -> dict[str, Any]:
        return jwt.decode(
            token,
            key,
            algorithms=self.algorithms,
            audience=self.audience,
            issuer=self.issuer,
            leeway=self.leeway,
            options={"require": self.required},
        )

    def _token(self, connection: HTTPConnection) -> str | None:
        scheme, _, token = connection.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
        if self.cookie is not None:
            values = cookie_values(connection.scope, self.cookie)
            if values:
                return values[0]
        return None


def _key(key: Secrets) -> Any:
    """A public or private key (PEM, or a key object), out of its SecretStr."""
    if isinstance(key, list | tuple):
        raise ValueError("only HMAC secrets rotate; give one key")
    return key.get_secret_value() if hasattr(key, "get_secret_value") else key


def _subject(claims: dict[str, Any]) -> str | None:
    subject = claims.get("sub")
    return str(subject) if subject is not None else None
