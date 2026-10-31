"""FastAPIAuth and its strategies: tried in order, for sessions, JWTs and OAuth2 logins."""

# pylint: disable=protected-access  # tests look at the internals they check

import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterator
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba import Cache, FastAPIAuth
from fastapi_alcazaba.auth import (
    AuthenticationFailed,
    FastAPIAPIKeys,
    HTTPBearerStrategy,
    SessionStrategy,
    Strategy,
    TokenBlocklist,
)
from fastapi_alcazaba.auth.strategies import JWTStrategy, OAuth2Strategy
from fastapi_alcazaba.auth.strategies.base import call
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.security import ServerSessionMiddleware
from fastapi_alcazaba.telemetry.metrics import Metrics

SECRET = "jwt-secret-of-at-least-32-bytes!!"  # noqa: S105  # the tests' own
USERS = {"ana": {"id": "ana", "name": "Ana"}, "ben": {"id": "ben", "name": "Ben"}}


class Fixed(Strategy):
    """Finds `user`, finds nobody (None), or fails, whatever the request."""

    def __init__(self, name: str, user: Any = None, fail: bool = False) -> None:
        super().__init__(name)
        self.user, self.fail, self.calls = user, fail, 0
        self.challenge = f"{name.title()} realm=test"

    async def authenticate(self, connection: HTTPConnection) -> Any:
        self.calls += 1
        if self.fail:
            raise AuthenticationFailed(f"{self.name} says no")
        return self.user


def app_with(auth: FastAPIAuth, *names: str, optional: bool = False) -> FastAPI:
    app = FastAPI()

    @app.get("/me")
    async def me(
        user: Annotated[Any, Depends(auth.authenticate(*names, optional=optional))],
    ) -> Any:
        return {"user": user}

    return app


class TestFastAPIAuth:
    def test_the_first_strategy_that_finds_a_user(self) -> None:
        first, second, third = Fixed("a"), Fixed("b", user="ben"), Fixed("c", user="cris")
        client = TestClient(app_with(FastAPIAuth(first, second, third), "a", "b", "c"))
        assert client.get("/me").json() == {"user": "ben"}
        assert (first.calls, second.calls, third.calls) == (1, 1, 0)

    def test_a_failure_goes_on_to_the_next_one(self, caplog: pytest.LogCaptureFixture) -> None:
        auth = FastAPIAuth(Fixed("a", fail=True), Fixed("b", user="ben"))
        with caplog.at_level(logging.INFO, logger="fastapi_alcazaba.auth.strategies.base"):
            assert TestClient(app_with(auth)).get("/me").json() == {"user": "ben"}
        assert [record.getMessage() for record in caplog.records] == [
            "Authentication by a failed: a says no"
        ]

    def test_nobody_gets_a_401_with_every_challenge(self) -> None:
        auth = FastAPIAuth(Fixed("a"), Fixed("b", fail=True))
        refused = TestClient(app_with(auth)).get("/me")
        assert (refused.status_code, refused.json()) == (401, {"detail": "Not authenticated"})
        assert refused.headers["www-authenticate"] == "A realm=test, B realm=test"

    def test_only_the_strategies_named(self) -> None:
        auth = FastAPIAuth(Fixed("a", user="ana"), Fixed("b", user="ben"))
        assert TestClient(app_with(auth, "b")).get("/me").json() == {"user": "ben"}

    def test_optional(self) -> None:
        auth = FastAPIAuth(Fixed("a", fail=True))
        assert TestClient(app_with(auth, optional=True)).get("/me").json() == {"user": None}

    def test_errors_are_not_failures(self) -> None:
        class Broken(Strategy):
            async def authenticate(self, connection: HTTPConnection) -> Any:
                raise ConnectionError("database down")

        client = TestClient(app_with(FastAPIAuth(Broken("broken"), Fixed("b", user="ben"))))
        with pytest.raises(ConnectionError):
            client.get("/me")

    def test_declaration_errors(self) -> None:
        auth = FastAPIAuth(Fixed("a"))
        with pytest.raises(KeyError, match="no strategy called 'b'"):
            auth.authenticate("b")
        with pytest.raises(ValueError, match="called 'a' already"):
            auth.use(Fixed("a"))
        with pytest.raises(ValueError, match="no strategies"):
            FastAPIAuth().authenticate()
        with pytest.raises(ValueError, match="needs a name"):
            Fixed("")

    def test_websockets_are_closed_with_1008(self) -> None:
        auth = FastAPIAuth(Fixed("a"))
        app = FastAPI()

        @app.websocket("/ws", dependencies=[Depends(auth.authenticate())])
        async def chat(websocket: WebSocket) -> None:
            await websocket.accept()

        with (
            pytest.raises(WebSocketDisconnect) as refused,
            TestClient(app).websocket_connect("/ws"),
        ):
            pass
        assert refused.value.code == 1008

    async def test_without_a_dependency(self) -> None:
        auth = FastAPIAuth(Fixed("a"), Fixed("b", user="ben"))
        connection = HTTPConnection({"type": "http", "headers": []})
        assert await auth.user(connection) == "ben"
        assert await auth.user(connection, "a") is None

    def test_a_strategy_of_your_own(self) -> None:
        """The documentation's: API keys of FastAPIAPIKeys as a strategy."""
        records: dict[str, Any] = {}
        keys: FastAPIAPIKeys[Any] = FastAPIAPIKeys(records.get)

        class APIKeyStrategy(Strategy):
            challenge = "APIKey"

            async def authenticate(self, connection: HTTPConnection) -> Any:
                key = connection.headers.get("x-api-key")
                if key is None:
                    return None
                try:
                    return await keys.authenticate(key)
                except HTTPException as exc:  # the 401 of FastAPIAPIKeys
                    raise AuthenticationFailed("invalid API key") from exc

        new = keys.generate(scopes=["read"])
        records[new.record.id] = new.record
        client = TestClient(app_with(FastAPIAuth(APIKeyStrategy("api-key"))))
        assert client.get("/me", headers={"X-API-Key": new.key}).json()["user"]["id"] == (
            new.record.id
        )
        assert client.get("/me", headers={"X-API-Key": "ak_wrong"}).status_code == 401

    def test_outcomes_are_counted(self) -> None:
        reader = InMemoryMetricReader()
        metrics = Metrics(meter_provider=MeterProvider(metric_readers=[reader]))
        auth = FastAPIAuth(Fixed("a", fail=True), Fixed("b", user="ben"), metrics=metrics)
        client = TestClient(app_with(auth))
        client.get("/me")
        assert TestClient(app_with(auth, "a")).get("/me").status_code == 401
        data = reader.get_metrics_data()
        assert data is not None
        counted = {
            tuple(sorted((point.attributes or {}).items())): getattr(point, "value", None)
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == "fastapi_alcazaba.auth"
            for point in metric.data.data_points
        }
        assert counted == {
            (("fastapi_alcazaba.outcome", "failure"), ("fastapi_alcazaba.strategy", "a")): 2,
            (("fastapi_alcazaba.outcome", "success"), ("fastapi_alcazaba.strategy", "b")): 1,
            (
                ("fastapi_alcazaba.outcome", "unauthenticated"),
                ("fastapi_alcazaba.strategy", "a"),
            ): 1,
        }


class TestCall:
    async def test_sync_async_and_async_callables(self) -> None:
        class AsyncCallable:  # an object with an async __call__
            async def __call__(self, x: int) -> int:
                return x + 1

        async def coroutine(x: int) -> int:
            return x * 2

        assert await call(lambda x: x - 1, 5) == 4
        assert await call(coroutine, 5) == 10
        assert await call(AsyncCallable(), 5) == 6


def load_user(user_id: str) -> dict[str, str] | None:
    return USERS.get(user_id)


@pytest.fixture(name="sessions")
def fixture_sessions() -> Iterator[tuple[TestClient, SessionStrategy]]:
    sessions = SessionStrategy(load_user)
    auth = FastAPIAuth(sessions)
    app = app_with(auth)

    @app.post("/login/{user_id}")
    async def login(request: Request, user_id: str) -> None:
        sessions.login(request, user_id)

    @app.post("/logout")
    async def logout(request: Request) -> None:
        sessions.logout(request)

    Cache(InMemoryBackend(), prefix="test").install(app)
    app.add_middleware(ServerSessionMiddleware)
    with TestClient(app, base_url="https://testserver") as client:
        yield client, sessions


class TestSessionStrategy:
    def test_login_then_logout(self, sessions: tuple[TestClient, SessionStrategy]) -> None:
        client, _ = sessions
        assert client.get("/me").status_code == 401
        client.post("/login/ana")
        assert client.get("/me").json() == {"user": USERS["ana"]}
        client.post("/logout")
        assert client.get("/me").status_code == 401

    def test_login_gives_the_session_a_new_id(
        self,
        sessions: tuple[TestClient, SessionStrategy],
    ) -> None:
        client, _ = sessions
        client.post("/login/ana")
        before = client.cookies["__Host-session"]
        client.post("/login/ben")
        assert client.cookies["__Host-session"] != before
        assert client.get("/me").json() == {"user": USERS["ben"]}

    def test_a_user_that_is_gone(self, sessions: tuple[TestClient, SessionStrategy]) -> None:
        client, _ = sessions
        client.post("/login/someone-deleted")
        assert client.get("/me").status_code == 401

    def test_any_session_middleware(self) -> None:
        sessions = SessionStrategy(load_user, key="uid")
        app = app_with(FastAPIAuth(sessions))

        @app.post("/logout")
        async def logout(request: Request) -> dict[str, Any]:
            sessions.logout(request)
            return dict(request.scope["session"])

        def plain_sessions(inner: ASGIApp) -> ASGIApp:
            async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
                scope["session"] = {"uid": "ben", "cart": [1]}
                await inner(scope, receive, send)

            return middleware

        client = TestClient(plain_sessions(app))
        assert client.get("/me").json() == {"user": USERS["ben"]}
        assert client.post("/logout").json() == {}

    def test_needs_a_session_middleware(self) -> None:
        client = TestClient(app_with(FastAPIAuth(SessionStrategy(load_user))))
        with pytest.raises(RuntimeError, match="needs ServerSessionMiddleware"):
            client.get("/me")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestJWTStrategy:
    def test_issued_tokens_authenticate(self) -> None:
        strategy = JWTStrategy(SECRET, verify=lambda claims: USERS.get(claims["sub"]))
        client = TestClient(app_with(FastAPIAuth(strategy)))
        assert client.get("/me", headers=bearer(strategy.issue("ana"))).json() == {
            "user": USERS["ana"]
        }
        refused = client.get("/me", headers=bearer(strategy.issue("nobody")))
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"] == "Bearer"

    async def test_the_claims_by_default(self) -> None:
        strategy = JWTStrategy(SECRET, issuer="shop", audience="api")
        claims = await strategy.decode(strategy.issue("ana", expires_in=60, role="admin"))
        assert claims["sub"] == "ana"
        assert (claims["iss"], claims["aud"], claims["role"]) == ("shop", "api", "admin")
        assert claims["exp"] - claims["iat"] == 60
        assert len(claims["jti"]) >= 20

    @pytest.mark.parametrize(
        "claims",
        [
            {"sub": "ana", "exp": int(time.time()) - 10},  # expired
            {"sub": "ana"},  # no exp
            {"exp": int(time.time()) + 60},  # no sub
            {"sub": "ana", "exp": int(time.time()) + 60, "nbf": int(time.time()) + 600},
        ],
    )
    async def test_invalid_claims(self, claims: dict[str, Any]) -> None:
        with pytest.raises(AuthenticationFailed, match="invalid token"):
            await JWTStrategy(SECRET).decode(jwt.encode(claims, SECRET, algorithm="HS256"))

    async def test_audience_and_issuer(self) -> None:
        strategy = JWTStrategy(SECRET, audience="api", issuer="shop")
        other = JWTStrategy(SECRET, audience="other", issuer="shop")
        with pytest.raises(AuthenticationFailed, match="InvalidAudienceError"):
            await strategy.decode(other.issue("ana"))
        elsewhere = JWTStrategy(SECRET, audience="api", issuer="elsewhere")
        with pytest.raises(AuthenticationFailed, match="InvalidIssuerError"):
            await strategy.decode(elsewhere.issue("ana"))

    async def test_tokens_for_another_audience_without_one(self) -> None:
        """A token meant for some other service isn't taken because no audience is set."""
        with pytest.raises(AuthenticationFailed, match="InvalidAudienceError"):
            await JWTStrategy(SECRET).decode(JWTStrategy(SECRET, audience="billing").issue("ana"))

    async def test_forged_tokens(self) -> None:
        strategy = JWTStrategy(SECRET)
        other = JWTStrategy("another-secret-of-at-least-32-bytes")
        with pytest.raises(AuthenticationFailed, match="InvalidSignatureError"):
            await strategy.decode(other.issue("ana"))
        header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=")
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "ana", "exp": int(time.time()) + 60}).encode()
        ).rstrip(b"=")
        with pytest.raises(AuthenticationFailed, match="invalid token"):
            await strategy.decode(f"{header.decode()}.{payload.decode()}.")
        with pytest.raises(AuthenticationFailed, match="too long"):
            await strategy.decode("x" * 10_000)

    async def test_secrets_rotate(self) -> None:
        old = JWTStrategy("the-old-secret-of-at-least-32-bytes!")
        new = JWTStrategy(["the-new-secret-of-at-least-32-bytes!", old._keys[0]])
        assert (await new.decode(old.issue("ana")))["sub"] == "ana"
        with pytest.raises(AuthenticationFailed):
            await old.decode(new.issue("ana"))  # new tokens use the first secret

    async def test_asymmetric_keys(self) -> None:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public = private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        strategy = JWTStrategy(public, algorithms=["RS256"], signing_key=pem)
        assert (await strategy.decode(strategy.issue("ana")))["sub"] == "ana"
        verifier = JWTStrategy(public, algorithms=["RS256"])
        with pytest.raises(RuntimeError, match="signing_key"):
            verifier.issue("ana")
        # The public key used as an HMAC secret (algorithm confusion) isn't accepted.
        forged = jwt.encode({"sub": "ana", "exp": int(time.time()) + 60}, "x" * 32)
        with pytest.raises(AuthenticationFailed):
            await verifier.decode(forged)

    async def test_jwks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
        jwks = {"keys": [{**jwk, "kid": "key-1", "use": "sig", "alg": "RS256"}]}
        monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: jwks)
        strategy = JWTStrategy(
            jwks_url="https://idp.example.com/.well-known/jwks.json",
            algorithms=["RS256"],
            audience="api",
            issuer="https://idp.example.com/",
        )
        token = jwt.encode(
            {
                "sub": "ana",
                "aud": "api",
                "iss": "https://idp.example.com/",
                "exp": int(time.time()) + 60,
            },
            private,
            algorithm="RS256",
            headers={"kid": "key-1"},
        )
        assert (await strategy.decode(token))["sub"] == "ana"
        unknown = jwt.encode(
            {
                "sub": "ana",
                "aud": "api",
                "iss": "https://idp.example.com/",
                "exp": int(time.time()) + 60,
            },
            private,
            algorithm="RS256",
            headers={"kid": "key-2"},
        )
        with pytest.raises(AuthenticationFailed, match="PyJWKClientError"):
            await strategy.decode(unknown)

    def test_revoked_tokens(self) -> None:
        cache = Cache(InMemoryBackend())
        blocklist = TokenBlocklist(cache)
        strategy = JWTStrategy(SECRET, blocklist=blocklist)
        auth = FastAPIAuth(strategy)
        app = app_with(auth)

        @app.post("/logout")
        async def logout(claims: Annotated[dict[str, Any], Depends(auth.authenticate())]) -> None:
            await blocklist.revoke(claims["jti"], expires_at=claims["exp"])

        client = TestClient(app)
        token = bearer(strategy.issue("ana"))
        assert client.get("/me", headers=token).status_code == 200
        client.post("/logout", headers=token)
        assert client.get("/me", headers=token).status_code == 401
        assert "jti" in strategy.required
        assert "iat" in strategy.required

    def test_from_a_cookie(self) -> None:
        strategy = JWTStrategy(SECRET, cookie="access_token")
        client = TestClient(app_with(FastAPIAuth(strategy)))
        client.cookies.set("access_token", strategy.issue("ana"))
        assert client.get("/me").json()["user"]["sub"] == "ana"
        client.cookies.clear()
        assert client.get("/me", headers={"Authorization": "Basic abc"}).status_code == 401

    def test_openapi(self) -> None:
        auth = FastAPIAuth(JWTStrategy(SECRET), Fixed("other"))
        schema = TestClient(app_with(auth)).get("/openapi.json").json()
        assert schema["paths"]["/me"]["get"]["security"] == [{"jwt": []}]
        assert schema["components"]["securitySchemes"]["jwt"] == {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({}, "key or jwks_url"),
            ({"key": SECRET, "jwks_url": "https://idp/jwks"}, "key or jwks_url"),
            ({"jwks_url": "https://idp/jwks", "issuer": "https://idp/"}, "the audience"),
            ({"jwks_url": "https://idp/jwks", "audience": "api"}, "the issuer"),
            ({"key": SECRET, "leeway": 120, "blocklist": TokenBlocklist()}, "leeway=120"),
            ({"key": SECRET, "algorithms": ["none"]}, "'none' is never"),
            ({"key": SECRET, "algorithms": []}, "algorithms"),
            ({"key": SECRET, "algorithms": ["HS256", "RS256"]}, "don't mix"),
            ({"key": "short"}, "at least 32 bytes"),
            ({"key": ["a", "b"], "algorithms": ["RS256"]}, "only HMAC secrets rotate"),
        ],
    )
    def test_configuration_errors(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            JWTStrategy(**options)


class Provider:
    """Stands in for an OAuth2 provider's token and userinfo endpoints."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_status = 200
        self.profile_status = 200
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("no route to host", request=request)
        if request.url.path == "/token":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "bad_verification_code"})
            tokens = {"access_token": "at-123", "token_type": "bearer"}
            return httpx.Response(200, json=tokens)
        if request.headers.get("authorization") != "Bearer at-123":
            return httpx.Response(401)
        return httpx.Response(self.profile_status, json={"login": "ana", "id": 7})


PROVIDER: dict[str, Any] = {
    "name": "example",
    "authorization_url": "https://idp.example.com/authorize",
    "token_url": "https://idp.example.com/token",
    "userinfo_url": "https://idp.example.com/user",
    "client_id": "the client",
    "client_secret": "s3cret:&",
    "redirect_uri": "https://testserver/callback",
    "scopes": ["read:user", "email"],
}


def oauth2(provider: Provider, **options: Any) -> OAuth2Strategy:
    client = httpx.AsyncClient(transport=httpx.MockTransport(provider.handler))
    return OAuth2Strategy(**(PROVIDER | {"client": client} | options))


def oauth2_app(strategy: OAuth2Strategy) -> TestClient:
    auth = FastAPIAuth(strategy)
    app = FastAPI()

    @app.get("/login")
    async def login() -> Any:
        return strategy.redirect(prompt="consent")

    @app.get("/callback")
    async def callback(user: Annotated[Any, Depends(auth.authenticate("example"))]) -> Any:
        return {"user": user}

    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def log_in(client: TestClient) -> tuple[dict[str, list[str]], str]:
    """The provider's login page's query, and the state cookie."""
    redirect = client.get("/login")
    assert redirect.status_code == 302
    return parse_qs(urlsplit(redirect.headers["location"]).query), redirect.headers["set-cookie"]


class TestOAuth2Strategy:
    def test_the_redirect_to_the_provider(self) -> None:
        client = oauth2_app(oauth2(Provider()))
        redirect = client.get("/login")
        location = urlsplit(redirect.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == (
            "https://idp.example.com/authorize"
        )
        query = parse_qs(location.query)
        assert {key: values[0] for key, values in query.items() if key != "code_challenge"} == {
            "response_type": "code",
            "client_id": "the client",
            "redirect_uri": "https://testserver/callback",
            "state": query["state"][0],
            "code_challenge_method": "S256",
            "scope": "read:user email",
            "prompt": "consent",
        }
        cookie = redirect.headers["set-cookie"]
        name, _, rest = cookie.partition("=")
        value = rest.split(";")[0]
        state, _, verifier = value.partition(".")
        assert name == "__Host-alcazaba-oauth2-example"
        assert state == query["state"][0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        assert query["code_challenge"][0] == challenge.decode().rstrip("=")
        for attribute in ("Max-Age=600", "Path=/", "HttpOnly", "Secure", "SameSite=Lax"):
            assert attribute in cookie
        assert redirect.headers["cache-control"] == "no-store"

    def test_the_callback_logs_in(self) -> None:
        provider = Provider()
        seen: list[tuple[Any, Any]] = []

        def verify(tokens: dict[str, Any], profile: dict[str, Any] | None) -> dict[str, Any]:
            seen.append((tokens, profile))
            return {"id": "ana"}

        client = oauth2_app(oauth2(provider, verify=verify))
        query, _ = log_in(client)
        callback = client.get("/callback", params={"code": "the-code", "state": query["state"][0]})
        assert callback.json() == {"user": {"id": "ana"}}
        assert seen == [
            ({"access_token": "at-123", "token_type": "bearer"}, {"login": "ana", "id": 7})
        ]
        token_request, profile_request = provider.requests[0], provider.requests[1]
        form = parse_qs(token_request.content.decode())
        assert form["grant_type"] == ["authorization_code"]
        assert form["code"] == ["the-code"]
        assert form["redirect_uri"] == ["https://testserver/callback"]
        verifier = client.cookies["__Host-alcazaba-oauth2-example"].partition(".")[2]
        assert form["code_verifier"] == [verifier]
        assert "client_secret" not in form
        credentials = base64.b64decode(token_request.headers["authorization"].split()[1])
        assert credentials == b"the%20client:s3cret%3A%26"  # each part form-encoded
        assert profile_request.headers["authorization"] == "Bearer at-123"

    def test_client_credentials_in_the_form(self) -> None:
        provider = Provider()
        client = oauth2_app(oauth2(provider, client_auth="post"))
        query, _ = log_in(client)
        client.get("/callback", params={"code": "c", "state": query["state"][0]})
        form = parse_qs(provider.requests[0].content.decode())
        assert (form["client_id"], form["client_secret"]) == (["the client"], ["s3cret:&"])
        assert "authorization" not in provider.requests[0].headers

    def test_the_profile_by_default_or_the_tokens(self) -> None:
        client = oauth2_app(oauth2(Provider()))
        query, _ = log_in(client)
        user = client.get("/callback", params={"code": "c", "state": query["state"][0]})
        assert user.json() == {"user": {"login": "ana", "id": 7}}
        client = oauth2_app(oauth2(Provider(), userinfo_url=None))
        query, _ = log_in(client)
        user = client.get("/callback", params={"code": "c", "state": query["state"][0]})
        assert user.json()["user"] == {"access_token": "at-123", "token_type": "bearer"}

    @pytest.mark.parametrize(
        "change",
        [
            lambda client, query: {"code": "c", "state": "another state"},
            lambda client, query: {"code": "c"},
            lambda client, query: client.cookies.clear() or {"code": "c", "state": query},
            lambda client, query: {"error": "access_denied", "state": query},
            lambda client, query: {"state": query},  # no code: not a callback
        ],
    )
    def test_refused_callbacks(self, change: Callable[[TestClient, str], dict[str, str]]) -> None:
        provider = Provider()
        client = oauth2_app(oauth2(provider))
        query, _ = log_in(client)
        refused = client.get("/callback", params=change(client, query["state"][0]))
        assert refused.status_code == 401
        assert provider.requests == []  # the code was never sent

    @pytest.mark.parametrize("failure", ["token_status", "profile_status"])
    def test_the_provider_refuses(self, failure: str) -> None:
        provider = Provider()
        setattr(provider, failure, 400)
        client = oauth2_app(oauth2(provider))
        query, _ = log_in(client)
        refused = client.get("/callback", params={"code": "c", "state": query["state"][0]})
        assert refused.status_code == 401

    def test_verify_refuses(self) -> None:
        client = oauth2_app(oauth2(Provider(), verify=lambda tokens, profile: None))
        query, _ = log_in(client)
        refused = client.get("/callback", params={"code": "c", "state": query["state"][0]})
        assert refused.status_code == 401

    def test_the_provider_is_down(self) -> None:
        provider = Provider()
        provider.down = True
        client = oauth2_app(oauth2(provider))
        query, _ = log_in(client)
        failed = client.get("/callback", params={"code": "c", "state": query["state"][0]})
        assert (failed.status_code, failed.json()) == (
            502,
            {"detail": "The login provider didn't answer"},
        )

    def test_without_https_for_development(self) -> None:
        local = OAuth2Strategy(
            **PROVIDER
            | {
                "name": "dev",
                "authorization_url": "http://localhost:8080/authorize",
                "token_url": "http://127.0.0.1:8080/token",
                "userinfo_url": "http://[::1]:8080/user",
                "secure": False,
            }
        )
        assert local.cookie.name == "alcazaba-oauth2-dev"
        with pytest.raises(ValueError, match="HTTPS"):
            OAuth2Strategy(**PROVIDER | {"authorization_url": "http://idp.example.com/authorize"})
        with pytest.raises(ValueError, match="basic or post"):
            OAuth2Strategy(**PROVIDER | {"client_auth": "jwt"})

    def test_a_client_of_its_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = Provider()
        real = httpx.AsyncClient

        def mocked(**options: Any) -> httpx.AsyncClient:
            assert options == {"timeout": 10.0}
            return real(transport=httpx.MockTransport(provider.handler), **options)

        monkeypatch.setattr(httpx, "AsyncClient", mocked)
        client = oauth2_app(OAuth2Strategy(**PROVIDER))
        query, _ = log_in(client)
        user = client.get("/callback", params={"code": "c", "state": query["state"][0]})
        assert user.json() == {"user": {"login": "ana", "id": 7}}

    def test_answers_that_are_not_json(self) -> None:
        def html(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>maintenance</html>")

        client = httpx.AsyncClient(transport=httpx.MockTransport(html))
        web = oauth2_app(OAuth2Strategy(**PROVIDER | {"client": client}))
        query, _ = log_in(web)
        refused = web.get("/callback", params={"code": "c", "state": query["state"][0]})
        assert refused.status_code == 401


def test_unknown_names_of_the_package() -> None:
    from fastapi_alcazaba.auth import strategies

    with pytest.raises(AttributeError, match="no attribute 'LDAPStrategy'"):
        _ = strategies.LDAPStrategy


TOKENS = {"opaque-token-of-ana": "ana", "b64+token/with=": "ben"}


async def find_token(token: str) -> dict[str, str] | None:
    user_id = TOKENS.get(token)
    return USERS[user_id] if user_id else None


class TestHTTPBearerStrategy:
    def test_verify_finds_the_user(self) -> None:
        client = TestClient(app_with(FastAPIAuth(HTTPBearerStrategy(find_token))))
        assert client.get("/me", headers=bearer("opaque-token-of-ana")).json() == {
            "user": USERS["ana"]
        }
        assert client.get("/me", headers=bearer("b64+token/with=")).json() == {"user": USERS["ben"]}

    @pytest.mark.parametrize(
        "authorization",
        ["Bearer unknown-token", "Bearer ", "Bearer two tokens", "Bearer " + "a" * 5_000],
    )
    def test_refused(self, authorization: str) -> None:
        seen: list[str] = []

        def verify(token: str) -> None:
            seen.append(token)

        client = TestClient(app_with(FastAPIAuth(HTTPBearerStrategy(verify, realm="shop"))))
        refused = client.get("/me", headers={"Authorization": authorization})
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"] == 'Bearer realm="shop"'
        # Only well-formed tokens reach verify().
        assert seen == (["unknown-token"] if authorization == "Bearer unknown-token" else [])

    def test_other_schemes_are_not_its(self) -> None:
        strategy = HTTPBearerStrategy(find_token)
        client = TestClient(app_with(FastAPIAuth(strategy, Fixed("other", user="cris"))))
        assert client.get("/me", headers={"Authorization": "Basic YW5hOnB3"}).json() == {
            "user": "cris"
        }

    def test_with_jwts(self) -> None:
        """A JWT that JWTStrategy refuses goes on to the opaque tokens, and the other way round."""
        jwts = JWTStrategy(SECRET)
        auth = FastAPIAuth(jwts, HTTPBearerStrategy(find_token))
        client = TestClient(app_with(auth))
        assert client.get("/me", headers=bearer(jwts.issue("ana"))).json()["user"]["sub"] == "ana"
        assert client.get("/me", headers=bearer("opaque-token-of-ana")).json() == {
            "user": USERS["ana"]
        }
        refused = client.get("/me", headers=bearer("nothing"))
        assert refused.headers["www-authenticate"] == "Bearer, Bearer"
        schema = client.get("/openapi.json").json()
        assert schema["paths"]["/me"]["get"]["security"] == [{"jwt": []}, {"bearer": []}]

    def test_the_realm_is_checked(self) -> None:
        with pytest.raises(ValueError, match="realm"):
            HTTPBearerStrategy(find_token, realm='say "hi"')
