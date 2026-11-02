"""Logging in with an OAuth2 provider (GitHub, Google, Keycloak...), by the authorization code
flow with PKCE: pip install "fastapi-alcazaba[auth-strategy-oauth2]".

    github = auth.use(OAuth2Strategy(
        name="github",
        authorization_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        userinfo_url="https://api.github.com/user",
        client_id=settings.github_client_id,
        client_secret=settings.github_client_secret,
        redirect_uri="https://example.com/auth/github/callback",
        scopes=["read:user"],
        verify=find_or_create_user,  # (tokens, profile) -> your user
    ))

    @app.get("/auth/github")
    async def login() -> RedirectResponse:
        return github.redirect()  # to GitHub, which sends the user back to the callback

    @app.get("/auth/github/callback")
    async def callback(request: Request, user: GitHubUser) -> RedirectResponse:
        sessions.login(request, user.id)  # with a SessionStrategy
        return RedirectResponse("/", status_code=303)

where `GitHubUser = Annotated[User, Depends(auth.authenticate("github"))]`.
"""

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlsplit

from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import HTTPConnection
from starlette.responses import RedirectResponse
from starlette.status import HTTP_502_BAD_GATEWAY

from fastapi_alcazaba.auth.strategies.base import AuthenticationFailed, Strategy, call
from fastapi_alcazaba.security._cookies import CookieSettings, cookie_values

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover  # httpx under its old name
    try:
        import httpx  # type: ignore[no-redef,import-not-found]
    except ImportError as exc:
        raise ImportError(
            'OAuth2Strategy needs httpx2: pip install "fastapi-alcazaba[auth-strategy-oauth2]"'
        ) from exc

#: Seconds the user has to log in at the provider and come back.
STATE_LIFETIME = 600


class AuthorizationCodeFlow(Strategy):
    """The authorization code flow with PKCE, which `OAuth2Strategy` and `OIDCStrategy` share:
    `redirect()` sends the browser to the provider, and the callback's request is authenticated
    by its code. Subclasses set the endpoints and implement `_finish`.

    `redirect()` starts the login: it sends the browser to the provider with a random `state`
    and a PKCE challenge, whose verifier waits in a short-lived cookie (HttpOnly, SameSite=Lax,
    `__Host-` when Secure). A callback without that cookie or with another state is refused:
    nobody can log a user in with their own account (login CSRF) nor use a stolen code.
    """

    logs_in = True

    def __init__(
        self,
        *,
        name: str,
        client_id: str,
        client_secret: str | SecretStr,
        redirect_uri: str,
        scopes: Sequence[str],
        client_auth: Literal["basic", "post"],
        params: Mapping[str, str] | None,
        secure: bool,
        timeout: float,
        client: Any,
    ) -> None:
        super().__init__(name)
        if client_auth not in ("basic", "post"):
            raise ValueError("client_auth is basic or post")
        self.authorization_url: str | None = None
        self.token_url: str | None = None
        self.userinfo_url: str | None = None
        self.client_id = client_id
        self.client_secret = (
            client_secret if isinstance(client_secret, SecretStr) else SecretStr(client_secret)
        )
        self.redirect_uri = redirect_uri
        self.scopes = list(scopes)
        self.client_auth = client_auth
        self.params = dict(params or {})
        self.timeout = timeout
        self.client = client
        prefix = "__Host-" if secure else ""
        self.cookie = CookieSettings(f"{prefix}alcazaba-oauth2-{name}", secure=secure)

    def redirect(self, **params: str) -> RedirectResponse:
        """The response that sends the browser to the provider's login page, with `params` added
        to the URL; it sets the cookie that the callback checks."""
        authorization_url = self._endpoint(self.authorization_url, "authorization")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        started, kept = self._start()
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "code_challenge": challenge.decode().rstrip("="),
            "code_challenge_method": "S256",
            **({"scope": " ".join(self.scopes)} if self.scopes else {}),
            **started,
            **self.params,
            **params,
        }
        separator = "&" if "?" in authorization_url else "?"
        response = RedirectResponse(
            f"{authorization_url}{separator}{urlencode(query)}", status_code=302
        )
        cookie = ".".join([state, verifier, *kept])
        name, value = self.cookie.header(cookie, max_age=STATE_LIFETIME)
        response.raw_headers.append((name, value))
        response.headers["Cache-Control"] = "no-store"
        return response

    async def authenticate(self, connection: HTTPConnection) -> Any:
        query = connection.query_params
        if "error" in query:
            raise AuthenticationFailed(f"the provider refused: {query['error'][:100]!r}")
        code, state = query.get("code"), query.get("state")
        if not code:
            return None
        stored = cookie_values(connection.scope, self.cookie.name)
        parts = stored[0].split(".") if stored else []
        if len(parts) < 2:  # no cookie, or not one this strategy set
            parts = ["", ""]
        expected, verifier, kept = parts[0], parts[1], parts[2:]
        if not (state and expected and hmac.compare_digest(state.encode(), expected.encode())):
            raise AuthenticationFailed("the state doesn't match the one sent (or expired)")
        tokens = await self._tokens(code, verifier)
        return await self._finish(tokens, kept)

    def _start(self) -> tuple[dict[str, str], list[str]]:
        """What the login page gets besides the code flow's parameters, and what the cookie
        keeps for the callback after the state and the verifier (tokens without dots)."""
        return {}, []

    async def _finish(self, tokens: dict[str, Any], kept: list[str]) -> Any:
        """The user of a login whose code gave `tokens`; `kept` is what `_start` kept."""
        raise NotImplementedError

    def _endpoint(self, url: str | None, what: str) -> str:
        if url is None:
            raise RuntimeError(f"{self.name}: the {what} endpoint isn't known yet")
        return url

    async def _tokens(self, code: str, verifier: str) -> dict[str, Any]:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
        }
        headers = {"Accept": "application/json"}
        secret = self.client_secret.get_secret_value()
        if self.client_auth == "basic":
            # RFC 6749, section 2.3.1: each part form-encoded first.
            credentials = f"{quote(self.client_id, safe='')}:{quote(secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
        else:
            form |= {"client_id": self.client_id, "client_secret": secret}
        token_url = self._endpoint(self.token_url, "token")
        response = await self._call("POST", token_url, data=form, headers=headers)
        tokens = _json(response)
        if response.status_code != 200 or not isinstance(tokens.get("access_token"), str):
            error = tokens.get("error", f"HTTP {response.status_code}")
            raise AuthenticationFailed(f"the code wasn't exchanged: {str(error)[:100]!r}")
        return tokens

    async def _profile(self, url: str, access_token: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        response = await self._call("GET", url, headers=headers)
        if response.status_code != 200:
            raise AuthenticationFailed(f"the profile wasn't read: HTTP {response.status_code}")
        return _json(response)

    async def _call(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            if self.client is not None:
                return await self.client.request(method, url, timeout=self.timeout, **kwargs)
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                return await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            # The provider's fault, not the user's: a 502, not a 401.
            raise HTTPException(HTTP_502_BAD_GATEWAY, "The login provider didn't answer") from exc


class OAuth2Strategy(AuthorizationCodeFlow):
    """Authenticates the requests that come back from the provider's login page (the callback):
    checks their `state`, exchanges their code for tokens, reads the user's profile, and returns
    the user that `verify` makes of them. Other requests carry nothing for it (None): it only
    goes on the callback route.

    `redirect()` starts the login: it sends the browser to the provider with a random `state`
    and a PKCE challenge, whose verifier waits in a short-lived cookie (HttpOnly, SameSite=Lax,
    `__Host-` when Secure). A callback without that cookie or with another state is refused:
    nobody can log a user in with their own account (login CSRF) nor use a stolen code.

    For OpenID Connect providers, `OIDCStrategy` also verifies the ID token they send.

    :param name: what `FastAPIAuth.authenticate` calls it by, and the cookie's name.
    :param authorization_url: the provider's login page.
    :param token_url: where codes are exchanged for tokens.
    :param client_id: the app's id at the provider.
    :param client_secret: the app's secret at the provider.
    :param redirect_uri: the callback's absolute URL, as registered at the provider.
    :param scopes: what to ask the provider for.
    :param userinfo_url: where to read the user's profile with the access token; None for none.
    :param verify: `verify(tokens, profile)`: your user, or None to refuse the login. `tokens` is
        the token endpoint's answer (`access_token`, `refresh_token`...), `profile` the JSON of
        `userinfo_url` (None without it). An `id_token` among them isn't verified: use
        `OIDCStrategy` to rely on its claims. Async, or a plain function, which runs in a thread.
        By default the user is the profile (or the tokens without it).
    :param client_auth: how the app authenticates to the token endpoint: `basic` (HTTP Basic,
        RFC 6749's default) or `post` (in the form).
    :param params: more parameters for the login page (`prompt`, `access_type`...).
    :param secure: the cookie is Secure (browsers accept it from http://localhost too).
    :param timeout: seconds for each call to the provider.
    :param client: the httpx2 client to call the provider with; one of its own by default.
    """

    def __init__(
        self,
        *,
        name: str,
        authorization_url: str,
        token_url: str,
        client_id: str,
        client_secret: str | SecretStr,
        redirect_uri: str,
        scopes: Sequence[str] = (),
        userinfo_url: str | None = None,
        verify: Callable[[dict[str, Any], dict[str, Any] | None], Any] | None = None,
        client_auth: Literal["basic", "post"] = "basic",
        params: Mapping[str, str] | None = None,
        secure: bool = True,
        timeout: float = 10.0,
        client: Any = None,
    ) -> None:
        super().__init__(
            name=name,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
            client_auth=client_auth,
            params=params,
            secure=secure,
            timeout=timeout,
            client=client,
        )
        for url in (authorization_url, token_url, userinfo_url):
            if url is not None and not _secure(url):
                raise ValueError(f"the provider's URLs are HTTPS (or http://localhost): {url}")
        self.authorization_url = authorization_url
        self.token_url = token_url
        self.userinfo_url = userinfo_url
        self.verify = verify

    async def _finish(self, tokens: dict[str, Any], kept: list[str]) -> Any:
        profile = None
        if self.userinfo_url is not None:
            profile = await self._profile(self.userinfo_url, tokens["access_token"])
        if self.verify is None:
            return profile if profile is not None else tokens
        user = await call(self.verify, tokens, profile)
        if user is None:
            raise AuthenticationFailed("verify() refused the login")
        return user


def _secure(url: str) -> bool:
    """HTTPS, or plain HTTP to this machine (a provider in development)."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")


def _json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
