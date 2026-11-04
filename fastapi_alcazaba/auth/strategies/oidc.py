"""Logging in with an OpenID Connect provider (Google, Microsoft Entra ID, Keycloak, Okta,
Auth0, Zitadel...), whose ID token is verified as OpenID Connect Core says:
pip install "fastapi-alcazaba[auth-strategy-oidc]".

    google = auth.use(OIDCStrategy(
        name="google",
        issuer="https://accounts.google.com",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri="https://example.com/auth/google/callback",
        verify=find_or_create_user,  # (claims, userinfo) -> your user
    ))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await google.discover()  # the provider's endpoints and keys, from its issuer
        yield

The login is OAuth2Strategy's (the authorization code flow with PKCE and a `state`), plus a
`nonce`; the ID token's signature (with the provider's JWKS), `iss`, `aud`, `azp`, `exp`, `iat`,
`nonce`, `at_hash` and `auth_time` are checked before `verify` sees its claims.

Logging out: `logout_url()` sends the browser to the provider's `end_session_endpoint`
(RP-Initiated Logout), and `backchannel_logout_endpoint()` takes the provider's logout tokens
(Back-Channel Logout) and ends the app's sessions of that login. The app may authenticate with a
private key (`client_auth="private_key_jwt"`) instead of a client secret.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode

from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response

from fastapi_alcazaba.auth.strategies.base import AuthenticationFailed, _secure, call
from fastapi_alcazaba.auth.strategies.oauth2 import AuthorizationCodeFlow, _json

try:
    import jwt
except ImportError as exc:  # pragma: no cover  # the extra isn't installed
    raise ImportError(
        'OIDCStrategy needs PyJWT: pip install "fastapi-alcazaba[auth-strategy-oidc]"'
    ) from exc

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

if TYPE_CHECKING:
    from fastapi_alcazaba.auth.strategies.jwt import JWTKey
    from fastapi_alcazaba.response_cache.caches import Cache
    from fastapi_alcazaba.security.sessions import UserSessions

#: The longest ID token (or logout token) read.
MAX_ID_TOKEN = 16_384
#: The event a logout token carries (OpenID Connect Back-Channel Logout 1.0, 2.4).
BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"
#: Seconds a logout token may be old (its `iat`), and its `jti` is remembered for replays.
LOGOUT_TOKEN_LIFETIME = 300
_CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


@dataclass(frozen=True, slots=True)
class LogoutToken:
    """A logout token the provider sent, verified: which login to end (`sid`), or every login of
    the user (`sub`, without `sid`)."""

    sub: str | None
    sid: str | None
    jti: str


#: Seconds between two fetches of the JWKS for a key it doesn't have: a token with a made-up `kid`
#: doesn't make the app fetch it on every request.
JWKS_REFETCH = 30
#: The hash of `at_hash` for each algorithm: the one of its signature (OpenID Connect Core,
#: 3.1.3.6; SHA-512 for EdDSA with Ed25519).
_AT_HASH = {
    "RS256": hashlib.sha256,
    "ES256": hashlib.sha256,
    "PS256": hashlib.sha256,
    "RS384": hashlib.sha384,
    "ES384": hashlib.sha384,
    "PS384": hashlib.sha384,
    "RS512": hashlib.sha512,
    "ES512": hashlib.sha512,
    "PS512": hashlib.sha512,
    "EdDSA": hashlib.sha512,
}


class OIDCStrategy(AuthorizationCodeFlow):
    """Authenticates the callback of an OpenID Connect login: exchanges its code for tokens,
    verifies the ID token, and returns the user that `verify` makes of its claims.

    The ID token is refused unless it's signed with one of `algorithms` by a key of the
    provider's JWKS (never `none`, never with the client secret), comes from `issuer`, is meant
    for this app (`aud` has `client_id`, and `azp` is it when there are several audiences or an
    `azp`), hasn't expired (`exp`) nor was issued in the future (`iat`), carries the `nonce`
    the login sent, the `at_hash` of the access token when it has one, and an `auth_time` within
    `max_age` when it's set. A provider that sends no ID token is refused.

    Its endpoints and keys come from the issuer's discovery document
    (`/.well-known/openid-configuration`), read by `await discover()` in the lifespan; whose
    `issuer` must be `issuer` exactly. Endpoints given here aren't read from it, and with all of
    them (`authorization_url`, `token_url`, `jwks_url`) there's nothing to discover.

    The raw ID token of a login is in `request.state.id_token` once the callback is
    authenticated: keep it in the session for `logout_url(id_token_hint=...)`. The claims have
    the provider's session id (`sid`): keep it as `oidc_sid` in the session for back-channel
    logout to find it (`UserSessions`).

    :param name: what `FastAPIAuth.authenticate` calls it by, and the cookie's name.
    :param issuer: the provider's issuer identifier, as its ID tokens have it in `iss`.
    :param client_id: the app's id at the provider: the ID tokens' audience.
    :param client_secret: the app's secret at the provider; None with `private_key_jwt`.
    :param redirect_uri: the callback's absolute URL, as registered at the provider.
    :param scopes: what to ask for; `openid` is required.
    :param verify: `verify(claims, userinfo)`: your user, or None to refuse the login. `claims`
        are the ID token's, verified; `userinfo` the userinfo endpoint's (with `userinfo=True`;
        None otherwise), whose `sub` is the ID token's. Async, or a plain function, which runs
        in a thread. By default the user is the claims, with the userinfo's added.
    :param userinfo: also read the userinfo endpoint, for claims the ID token doesn't carry.
    :param max_age: seconds since the user last entered their credentials at the provider, at
        most: sent as `max_age`, and checked in `auth_time`.
    :param algorithms: the signature algorithms accepted.
    :param leeway: seconds of tolerance on `exp`, `iat` and `auth_time`, for clocks out of step.
    :param authorization_url: the login page, instead of discovery's.
    :param token_url: where codes are exchanged for tokens, instead of discovery's.
    :param userinfo_url: the userinfo endpoint, instead of discovery's.
    :param jwks_url: the provider's keys, instead of discovery's.
    :param jwks_lifespan: seconds the keys are kept before they're fetched again.
    :param jwks_stale: seconds more they're used while the provider's keys can't be fetched.
        Fetches are one at a time, and at most every 30 seconds, failed ones too: tokens with
        made-up `kid`s (at the back-channel logout endpoint, which anyone can call) don't make
        the app call the provider for each.
    :param client_auth: how the app authenticates to the token endpoint: `basic` or `post` (with
        the client secret), or `private_key_jwt` (a JWT signed with `client_key`, RFC 7523: no
        shared secret; the provider has the public key, e.g. from the app's `jwks()`).
    :param client_key: the private key of `private_key_jwt`: a `JWTKey` (RS256, ES256, EdDSA...).
    :param post_logout_redirect_uris: where `logout_url` may send the browser back to, as
        registered at the provider; any other is refused.
    :param end_session_url: the provider's logout page, instead of discovery's.
    :param params: more parameters for the login page (`prompt`, `login_hint`...).
    :param secure: the cookie is Secure (browsers accept it from http://localhost too).
    :param timeout: seconds for each call to the provider.
    :param client: the httpx2 client to call the provider with; one of its own by default.
    """

    def __init__(
        self,
        *,
        name: str,
        issuer: str,
        client_id: str,
        client_secret: str | SecretStr | None = None,
        redirect_uri: str,
        scopes: Sequence[str] = ("openid", "email", "profile"),
        verify: Callable[[dict[str, Any], dict[str, Any] | None], Any] | None = None,
        userinfo: bool = False,
        max_age: int | None = None,
        algorithms: Sequence[str] = ("RS256", "ES256", "PS256", "EdDSA"),
        leeway: float = 60,
        authorization_url: str | None = None,
        token_url: str | None = None,
        userinfo_url: str | None = None,
        jwks_url: str | None = None,
        jwks_lifespan: float = 3600,
        jwks_stale: float = 3600,
        client_auth: Literal["basic", "post", "private_key_jwt"] = "basic",
        client_key: "JWTKey | None" = None,
        post_logout_redirect_uris: Sequence[str] = (),
        end_session_url: str | None = None,
        params: Mapping[str, str] | None = None,
        secure: bool = True,
        timeout: float = 10.0,
        client: Any = None,
    ) -> None:
        if client_auth not in ("basic", "post", "private_key_jwt"):
            raise ValueError("client_auth is basic, post or private_key_jwt")
        if client_auth == "private_key_jwt":
            if client_key is None or not client_key.can_sign or client_key.symmetric:
                raise ValueError("private_key_jwt signs with client_key: an asymmetric private key")
        elif client_secret is None:
            raise ValueError(f"client_auth={client_auth!r} needs the client_secret")
        if "openid" not in scopes:
            raise ValueError("an OpenID Connect login asks for the openid scope")
        if not algorithms or any(algorithm not in _AT_HASH for algorithm in algorithms):
            # HS256 would make the client secret, which the provider knows, a signing key.
            raise ValueError("the algorithms are asymmetric ones: RS256, ES256, PS256, EdDSA...")
        if max_age is not None and max_age < 0:
            raise ValueError("max_age can't be negative")
        if jwks_lifespan <= 0 or jwks_stale < 0:
            raise ValueError("jwks_lifespan is positive and jwks_stale not negative")
        if "nonce" in (params or {}):
            raise ValueError("the nonce is the strategy's: each login gets a new one")
        super().__init__(
            name=name,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
            client_auth=client_auth,
            params={**({"max_age": str(max_age)} if max_age is not None else {}), **(params or {})},
            secure=secure,
            timeout=timeout,
            client=client,
        )
        for url in (issuer, authorization_url, token_url, userinfo_url, jwks_url, end_session_url):
            if url is not None and not _secure(url):
                raise ValueError(f"the provider's URLs are HTTPS (or http://localhost): {url}")
        self.client_key = client_key
        self.post_logout_redirect_uris = list(post_logout_redirect_uris)
        self.end_session_url = end_session_url
        self.issuer = issuer
        self.verify = verify
        self.userinfo = userinfo
        self.max_age = max_age
        self.algorithms = list(algorithms)
        self.leeway = leeway
        self.authorization_url = authorization_url
        self.token_url = token_url
        self.userinfo_url = userinfo_url
        self.jwks_url = jwks_url
        self.jwks_lifespan = jwks_lifespan
        self.jwks_stale = jwks_stale
        self._keys: dict[str | None, jwt.PyJWK] = {}
        self._keys_fetched = -float("inf")
        self._keys_attempted = -float("inf")
        self._keys_error: Exception | None = None
        self._keys_lock = asyncio.Lock()

    async def discover(self) -> None:
        """Read the provider's discovery document: its endpoints and where its keys are, for
        those not given. Raises RuntimeError when it can't be read, or doesn't fit."""
        url = f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
        try:
            response = await self._call("GET", url, headers={"Accept": "application/json"})
        except HTTPException as exc:  # the 502 of _call, here outside a request
            raise RuntimeError(f"{self.name}: {url} didn't answer") from exc
        config = _json(response)
        if response.status_code != 200 or not config:
            raise RuntimeError(f"{self.name}: {url} answered {response.status_code}")
        if config.get("issuer") != self.issuer:
            # A document of another issuer: its tokens would be accepted as this one's.
            raise RuntimeError(
                f"{self.name}: the discovery document's issuer is {config.get('issuer')!r}, "
                f"not {self.issuer!r}"
            )
        found: dict[str, str | None] = {}
        for attribute, key in (
            ("authorization_url", "authorization_endpoint"),
            ("token_url", "token_endpoint"),
            ("userinfo_url", "userinfo_endpoint"),
            ("jwks_url", "jwks_uri"),
            ("end_session_url", "end_session_endpoint"),
        ):
            endpoint = config.get(key)
            if endpoint is not None and (not isinstance(endpoint, str) or not _secure(endpoint)):
                raise RuntimeError(f"{self.name}: {key} isn't an HTTPS URL: {endpoint!r}")
            found[attribute] = getattr(self, attribute) or endpoint
        for attribute in ("authorization_url", "token_url", "jwks_url"):
            if found[attribute] is None:
                raise RuntimeError(f"{self.name}: the discovery document has no {attribute}")
        if self.userinfo and found["userinfo_url"] is None:
            raise RuntimeError(f"{self.name}: userinfo=True, and the provider has no endpoint")
        for attribute, endpoint in found.items():
            setattr(self, attribute, endpoint)

    def _start(self) -> tuple[dict[str, str], list[str]]:
        self._endpoint(self.jwks_url, "JWKS")  # an ID token can't be verified without it
        nonce = secrets.token_urlsafe(32)
        return {"nonce": nonce}, [nonce]

    async def _finish(
        self,
        tokens: dict[str, Any],
        kept: list[str],
        connection: HTTPConnection,
    ) -> Any:
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise AuthenticationFailed("the provider sent no ID token")
        claims = await self.verify_id_token(
            id_token, nonce=kept[0] if kept else "", access_token=tokens["access_token"]
        )
        # For the app to keep: logout_url(id_token_hint=...) sends it back to the provider.
        connection.state.id_token = id_token
        userinfo = None
        if self.userinfo:
            url = self._endpoint(self.userinfo_url, "userinfo")
            userinfo = await self._profile(url, tokens["access_token"])
            if userinfo.get("sub") != claims["sub"]:
                # OpenID Connect Core, 5.3.4: the userinfo of someone else is a substitution.
                raise AuthenticationFailed("the userinfo is another subject's")
        if self.verify is None:
            return {**(userinfo or {}), **claims}
        user = await call(self.verify, claims, userinfo)
        if user is None:
            raise AuthenticationFailed("verify() refused the login")
        return user

    def _authenticate_client(self, form: dict[str, str], headers: dict[str, str], url: str) -> None:
        if self.client_auth != "private_key_jwt":
            super()._authenticate_client(form, headers, url)
            return
        assert self.client_key is not None  # noqa: S101  # checked when it was set up
        now = int(time.time())
        assertion = self.client_key.encode(
            {
                "iss": self.client_id,
                "sub": self.client_id,
                "aud": url,  # the token endpoint, as OpenID Connect Core 9 says
                "jti": secrets.token_urlsafe(16),
                "iat": now,
                "exp": now + 60,
            }
        )
        form |= {
            "client_id": self.client_id,
            "client_assertion_type": _CLIENT_ASSERTION_TYPE,
            "client_assertion": assertion,
        }

    def logout_url(
        self,
        *,
        id_token_hint: str | None = None,
        logout_hint: str | None = None,
        post_logout_redirect_uri: str | None = None,
        state: str | None = None,
    ) -> str:
        """The provider's logout page (RP-Initiated Logout 1.0) for the browser to go to after the
        app ended its own session. `id_token_hint` is the login's ID token
        (`request.state.id_token` at the callback): the provider then knows whose session to end
        without asking. `post_logout_redirect_uri` must be one of `post_logout_redirect_uris`;
        `state` comes back with it."""
        url = self._endpoint(self.end_session_url, "end_session")
        if (
            post_logout_redirect_uri is not None
            and post_logout_redirect_uri not in self.post_logout_redirect_uris
        ):
            raise ValueError(
                "post_logout_redirect_uri isn't one of post_logout_redirect_uris: an open "
                "redirect otherwise"
            )
        query = {
            "client_id": self.client_id,
            **({"id_token_hint": id_token_hint} if id_token_hint is not None else {}),
            **({"logout_hint": logout_hint} if logout_hint is not None else {}),
            **(
                {"post_logout_redirect_uri": post_logout_redirect_uri}
                if post_logout_redirect_uri is not None
                else {}
            ),
            **({"state": state} if state is not None else {}),
        }
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{urlencode(query)}"

    async def verify_logout_token(self, logout_token: str) -> LogoutToken:
        """The logout token's claims, verified as Back-Channel Logout 1.0 (2.6) says: signed by a
        key of the provider's JWKS with one of `algorithms`, from `issuer`, for this app, issued
        in the last LOGOUT_TOKEN_LIFETIME seconds, with a `jti`, the logout event, a `sub` or a
        `sid`, and no `nonce`. Raises AuthenticationFailed otherwise. Replays aren't checked
        here: `backchannel_logout_endpoint` does."""
        if len(logout_token) > MAX_ID_TOKEN:
            raise AuthenticationFailed("the logout token is too long")
        try:
            header = jwt.get_unverified_header(logout_token)
        except jwt.PyJWTError as exc:
            raise AuthenticationFailed(f"invalid logout token ({type(exc).__name__})") from None
        algorithm = header.get("alg")
        if algorithm not in self.algorithms:
            raise AuthenticationFailed(
                f"the logout token's algorithm isn't accepted: {algorithm!r}"
            )
        if header.get("typ") not in (None, "JWT", "logout+jwt"):
            raise AuthenticationFailed("the logout token's typ isn't logout+jwt")
        key = await self._signing_key(header.get("kid"))
        try:
            claims: dict[str, Any] = jwt.decode(
                logout_token,
                key,
                algorithms=[algorithm],
                audience=self.client_id,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["iss", "aud", "iat", "jti"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationFailed(f"invalid logout token ({type(exc).__name__})") from None
        iat = claims["iat"]
        if (
            not isinstance(iat, int | float)
            or time.time() - iat > LOGOUT_TOKEN_LIFETIME + self.leeway
        ):
            raise AuthenticationFailed("the logout token is too old")
        events = claims.get("events")
        if not isinstance(events, dict) or not isinstance(
            events.get(BACKCHANNEL_LOGOUT_EVENT), dict
        ):
            raise AuthenticationFailed("the logout token has no back-channel logout event")
        if "nonce" in claims:
            raise AuthenticationFailed("a logout token has no nonce: an ID token was sent")
        sub, sid, jti = claims.get("sub"), claims.get("sid"), claims["jti"]
        if not (isinstance(sub, str) or isinstance(sid, str)) or not isinstance(jti, str):
            raise AuthenticationFailed("the logout token names no sub nor sid")
        return LogoutToken(
            sub if isinstance(sub, str) else None, sid if isinstance(sid, str) else None, jti
        )

    def backchannel_logout_endpoint(
        self,
        sessions: "UserSessions",
        *,
        user_id: Callable[[str], Awaitable[Any] | Any] | None = None,
        cache: "Cache | None" = None,
    ) -> Callable[[Request], Awaitable[Response]]:
        """The endpoint the provider POSTs logout tokens to (Back-Channel Logout 1.0), to mount
        at the URL registered there: `app.post("/auth/backchannel-logout")(endpoint)`.

        A valid token ends the app's sessions of that login: those whose `oidc_sid` is its `sid`
        (`UserSessions.end_sid`: keep the ID token's `sid` there at login), or, for a token with
        a `sub` and no `sid`, every session of the user (`end_all`). `user_id(sub)` (sync or
        async) gives the app's user id of the provider's `sub` (the `sub` itself by default;
        None: nothing to end).
        Each token is taken once (its `jti`, kept in `cache`'s backend, the app's or
        FastAPICache's, for LOGOUT_TOKEN_LIFETIME seconds). Answers 200, or 400 with
        `invalid_request` (the reason is logged, not sent), with `Cache-Control: no-store`.
        """
        from fastapi_alcazaba.backends.tiered import shared_backend
        from fastapi_alcazaba.response_cache.caches import get_cache

        async def backchannel_logout(request: Request) -> Response:
            headers = {"Cache-Control": "no-store"}
            try:
                form = await request.form()
                token = form.get("logout_token")
                if not isinstance(token, str):
                    raise AuthenticationFailed("no logout_token")
                logout = await self.verify_logout_token(token)
                found = cache if cache is not None else get_cache(request)
                backend = shared_backend(found.backend)
                digest = hashlib.sha256(f"{self.issuer}\x00{logout.jti}".encode()).hexdigest()
                key = found.state_key("oidc_logout", digest)
                expire = 2 * (LOGOUT_TOKEN_LIFETIME + int(self.leeway))
                seen = await found.guard.run("incr", lambda: backend.incr(key, 1, expire))
                if seen > 1:
                    raise AuthenticationFailed("the logout token was used already")
            except AuthenticationFailed as exc:
                logger.warning("Refused a back-channel logout of %s: %s", self.name, exc)
                return JSONResponse({"error": "invalid_request"}, 400, headers=headers)
            if logout.sid is not None:  # that login only, not the user's others
                ended = await sessions.end_sid(logout.sid)
            else:
                assert logout.sub is not None  # noqa: S101  # verify_logout_token checked it
                user = await call(user_id, logout.sub) if user_id is not None else logout.sub
                ended = await sessions.end_all(user) if user is not None else 0
            logger.info("Back-channel logout of %s: %d sessions ended", self.name, ended)
            return Response(status_code=200, headers=headers)

        return backchannel_logout

    async def verify_id_token(
        self,
        id_token: str,
        *,
        nonce: str,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """The claims of `id_token`, verified as the class says; AuthenticationFailed when it
        isn't valid. `nonce` is the login's, and `access_token` the one it came with."""
        if len(id_token) > MAX_ID_TOKEN:
            raise AuthenticationFailed("the ID token is too long")
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise AuthenticationFailed(f"invalid ID token ({type(exc).__name__})") from None
        algorithm = header.get("alg")
        if algorithm not in self.algorithms:
            raise AuthenticationFailed(f"the ID token's algorithm isn't accepted: {algorithm!r}")
        key = await self._signing_key(header.get("kid"))  # PyJWT refuses a kid that isn't a str
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key,
                algorithms=[algorithm],
                audience=self.client_id,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationFailed(f"invalid ID token ({type(exc).__name__})") from None
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        if (len(audiences) > 1 or "azp" in claims) and claims.get("azp") != self.client_id:
            raise AuthenticationFailed("the ID token was given to another client (azp)")
        if not (nonce and hmac.compare_digest(str(claims.get("nonce", "")), nonce)):
            raise AuthenticationFailed("the ID token's nonce isn't the login's")
        if "at_hash" in claims and access_token is not None:
            digest = _AT_HASH[algorithm](access_token.encode()).digest()
            expected = base64.urlsafe_b64encode(digest[: len(digest) // 2]).decode().rstrip("=")
            if not hmac.compare_digest(str(claims["at_hash"]), expected):
                raise AuthenticationFailed("the ID token's at_hash isn't the access token's")
        if self.max_age is not None:
            auth_time = claims.get("auth_time")
            if not isinstance(auth_time, int | float) or isinstance(auth_time, bool):
                raise AuthenticationFailed("the ID token has no auth_time, and max_age is set")
            if time.time() - auth_time > self.max_age + self.leeway:
                raise AuthenticationFailed("the user signed in too long ago (max_age)")
        return claims

    async def _signing_key(self, kid: str | None) -> jwt.PyJWK:
        """The provider's key `kid`: from the JWKS kept, fetched again when it's old, or when
        the key isn't there (a rotation), at most every JWKS_REFETCH seconds."""
        if time.monotonic() - self._keys_fetched > self.jwks_lifespan or kid not in self._keys:
            await self._refresh_keys()
        if time.monotonic() - self._keys_fetched > self.jwks_lifespan + self.jwks_stale:
            if isinstance(self._keys_error, HTTPException):
                raise self._keys_error
            raise AuthenticationFailed(str(self._keys_error or "the provider's keys weren't read"))
        if kid is None and len(self._keys) == 1:
            return next(iter(self._keys.values()))  # a provider with one key may not name it
        key = self._keys.get(kid)
        if key is None:
            raise AuthenticationFailed(f"the ID token's key isn't the provider's: {kid!r}")
        return key

    async def _refresh_keys(self) -> None:
        # One fetch at a time, and one every JWKS_REFETCH seconds, failed ones too: those that
        # waited use what it got, or the keys kept.
        async with self._keys_lock:
            if time.monotonic() - self._keys_attempted < JWKS_REFETCH:
                return
            self._keys_attempted = time.monotonic()
            try:
                await self._fetch_keys()
            except (AuthenticationFailed, HTTPException) as exc:
                logger.warning("The provider's keys weren't fetched: %s", exc)
                self._keys_error = exc

    async def _fetch_keys(self) -> None:
        url = self._endpoint(self.jwks_url, "JWKS")
        response = await self._call("GET", url, headers={"Accept": "application/json"})
        if response.status_code != 200:
            status = response.status_code
            raise AuthenticationFailed(f"the provider's keys weren't read: HTTP {status}")
        try:
            jwks = jwt.PyJWKSet.from_dict(_json(response))
        except jwt.PyJWTError as exc:
            raise AuthenticationFailed(f"the provider's keys aren't a JWKS ({exc})") from None
        # Only signing keys: one for encryption (use=enc) isn't for checking signatures.
        self._keys = {key.key_id: key for key in jwks.keys if key.public_key_use in (None, "sig")}
        self._keys_fetched = time.monotonic()
