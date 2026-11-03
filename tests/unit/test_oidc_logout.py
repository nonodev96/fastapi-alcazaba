"""Logging out of an OpenID Connect provider: RP-Initiated Logout, Back-Channel Logout ending the
app's sessions, and private_key_jwt client authentication, against the fake provider."""

import time
import uuid
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from fastapi_alcazaba import Cache
from fastapi_alcazaba.auth import FastAPIAuth
from fastapi_alcazaba.auth.strategies.jwt import JWTKey
from fastapi_alcazaba.auth.strategies.oidc import BACKCHANNEL_LOGOUT_EVENT, OIDCStrategy
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.security import ServerSessionMiddleware, UserSessions
from tests.unit.test_auth_oidc import CLIENT_ID, ISSUER, SECRET, Provider, log_in, strategy

LOGGED_OUT = "https://testserver/logged-out"


class LogoutProvider(Provider):
    """The fake provider, with a logout page in its discovery document."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = super().handler(request)
        if request.url.path == "/.well-known/openid-configuration":
            document = response.json()
            document["end_session_endpoint"] = f"{ISSUER}/logout?ui=1"
            return httpx.Response(200, json=document)
        return response

    def logout_token(self, **claims: Any) -> str:
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "iat": now,
            "jti": uuid.uuid4().hex,
            "events": {BACKCHANNEL_LOGOUT_EVENT: {}},
            "sub": "user-1",
            "sid": "sid-a",
            **claims,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        headers = {"kid": "rsa-1", "typ": "logout+jwt"}
        return jwt.encode(payload, self.rsa, algorithm="RS256", headers=headers)


async def discovered(provider: Provider, **options: Any) -> OIDCStrategy:
    oidc = strategy(provider, **options)
    await oidc.discover()
    return oidc


class TestRPInitiatedLogout:
    async def test_the_logout_url(self) -> None:
        oidc = await discovered(LogoutProvider(), post_logout_redirect_uris=[LOGGED_OUT])
        url = oidc.logout_url(
            id_token_hint="the.id.token",  # noqa: S106  # not a secret
            post_logout_redirect_uri=LOGGED_OUT,
            state="s1",
        )
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{ISSUER}/logout"
        assert {key: values[0] for key, values in parse_qs(parts.query).items()} == {
            "ui": "1",
            "client_id": CLIENT_ID,
            "id_token_hint": "the.id.token",
            "post_logout_redirect_uri": LOGGED_OUT,
            "state": "s1",
        }
        assert parse_qs(urlsplit(oidc.logout_url(logout_hint="ana")).query) == {
            "ui": ["1"],
            "client_id": [CLIENT_ID],
            "logout_hint": ["ana"],
        }

    async def test_only_registered_redirects(self) -> None:
        oidc = await discovered(LogoutProvider(), post_logout_redirect_uris=[LOGGED_OUT])
        with pytest.raises(ValueError, match="open redirect"):
            oidc.logout_url(post_logout_redirect_uri="https://evil.example.com/")

    async def test_a_provider_without_a_logout_page(self) -> None:
        oidc = await discovered(Provider())
        with pytest.raises(RuntimeError, match="end_session endpoint isn't known"):
            oidc.logout_url()

    async def test_the_id_token_is_in_the_request_state(self) -> None:
        provider = LogoutProvider()
        oidc = await discovered(provider)
        auth = FastAPIAuth(oidc)
        app = FastAPI()

        @app.get("/login")
        async def login() -> Any:
            return oidc.redirect()

        @app.get("/callback")
        async def callback(
            request: Request,
            user: Annotated[Any, Depends(auth.authenticate("idp"))],
        ) -> Any:
            return {"sub": user["sub"], "id_token": request.state.id_token}

        client = TestClient(app, base_url="https://testserver", follow_redirects=False)
        answer = log_in(client, provider).json()
        assert (
            jwt.decode(answer["id_token"], options={"verify_signature": False})["sub"] == "user-1"
        )


class TestPrivateKeyJwt:
    async def test_the_token_request_carries_a_signed_assertion(self) -> None:
        provider = LogoutProvider()
        private = ec.generate_private_key(ec.SECP256R1())
        key = JWTKey(private, algorithm="ES256", kid="app-key-1")
        oidc = await discovered(
            provider, client_secret=None, client_auth="private_key_jwt", client_key=key
        )
        client = TestClient(_app(oidc), base_url="https://testserver", follow_redirects=False)
        assert log_in(client, provider).json()["sub"] == "user-1"
        (token_request,) = [r for r in provider.requests if r.url.path == "/token"]
        form = {
            name: values[0] for name, values in parse_qs(token_request.content.decode()).items()
        }
        assert "client_secret" not in form
        assert "authorization" not in token_request.headers
        assert form["client_id"] == CLIENT_ID
        assert (
            form["client_assertion_type"]
            == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        )
        assertion = form["client_assertion"]
        assert jwt.get_unverified_header(assertion)["kid"] == "app-key-1"
        claims = jwt.decode(
            assertion, private.public_key(), algorithms=["ES256"], audience=f"{ISSUER}/token"
        )
        assert (claims["iss"], claims["sub"]) == (CLIENT_ID, CLIENT_ID)
        assert 0 < claims["exp"] - claims["iat"] <= 60
        assert claims["jti"]

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"client_secret": None}, "needs the client_secret"),
            ({"client_auth": "private_key_jwt"}, "client_key"),
            (
                {
                    "client_auth": "private_key_jwt",
                    "client_key": JWTKey(
                        ec.generate_private_key(ec.SECP256R1()).public_key(), algorithm="ES256"
                    ),
                },
                "client_key",
            ),
            (
                {
                    "client_auth": "private_key_jwt",
                    "client_key": JWTKey("k" * 32, algorithm="HS256"),
                },
                "asymmetric",
            ),
            ({"client_auth": "jwt"}, "client_auth is"),
            ({"end_session_url": "http://idp.example.com/logout"}, "HTTPS"),
        ],
    )
    def test_configuration(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            strategy(Provider(), **options)


def _app(oidc: OIDCStrategy) -> FastAPI:
    auth = FastAPIAuth(oidc)
    app = FastAPI()

    @app.get("/login")
    async def login() -> Any:
        return oidc.redirect()

    @app.get("/callback")
    async def callback(user: Annotated[Any, Depends(auth.authenticate("idp"))]) -> Any:
        return user

    return app


class TestLogoutTokens:
    async def test_a_valid_one(self) -> None:
        provider = LogoutProvider()
        oidc = await discovered(provider)
        token = await oidc.verify_logout_token(provider.logout_token())
        assert (token.sub, token.sid) == ("user-1", "sid-a")
        only_sub = await oidc.verify_logout_token(provider.logout_token(sid=None))
        assert (only_sub.sub, only_sub.sid) == ("user-1", None)

    @pytest.mark.parametrize(
        ("claims", "message"),
        [
            ({"events": None}, "no back-channel logout event"),
            ({"events": {"other": {}}}, "no back-channel logout event"),
            ({"events": {BACKCHANNEL_LOGOUT_EVENT: "yes"}}, "no back-channel logout event"),
            ({"nonce": "n"}, "an ID token was sent"),
            ({"iat": int(time.time()) - 3600}, "too old"),
            ({"aud": "another-app"}, "InvalidAudienceError"),
            ({"iss": "https://elsewhere.example.com"}, "InvalidIssuerError"),
            ({"jti": None}, "MissingRequiredClaimError"),
            ({"sub": None, "sid": None}, "no sub nor sid"),
            ({"sub": 7, "sid": None}, "InvalidSubjectError"),
        ],
    )
    async def test_refused(self, claims: dict[str, Any], message: str) -> None:
        provider = LogoutProvider()
        oidc = await discovered(provider)
        with pytest.raises(Exception, match=message):
            await oidc.verify_logout_token(provider.logout_token(**claims))

    async def test_refused_headers(self) -> None:
        provider = LogoutProvider()
        oidc = await discovered(provider)
        payload = {"iss": ISSUER, "aud": CLIENT_ID, "iat": int(time.time()), "jti": "j"}
        hs = jwt.encode(payload, SECRET * 8, algorithm="HS256")
        wrong_typ = jwt.encode(
            payload, provider.rsa, algorithm="RS256", headers={"kid": "rsa-1", "typ": "at+jwt"}
        )
        for token, message in [
            ("not a token", "invalid logout token"),
            ("x" * 20_000, "too long"),
            (hs, "algorithm isn't accepted"),
            (wrong_typ, "typ"),
        ]:
            with pytest.raises(Exception, match=message):
                await oidc.verify_logout_token(token)


class Browser:
    """An app with OpenID Connect logins kept in server sessions, and its users' browsers."""

    def __init__(self, oidc: OIDCStrategy, signing_key: str | None = None, **endpoint: Any) -> None:
        self.cache = Cache(InMemoryBackend(), signing_key=signing_key)
        self.sessions = UserSessions(cache=self.cache)
        app = FastAPI()

        @app.post("/fake-login")
        async def fake_login(request: Request, user: str, sid: str) -> None:
            request.session["user_id"] = user
            request.session["oidc_sid"] = sid  # what the callback keeps of the ID token's claims

        @app.get("/me")
        async def me(request: Request) -> dict[str, Any]:
            return {"user": request.session.get("user_id")}

        app.post("/backchannel-logout")(
            oidc.backchannel_logout_endpoint(self.sessions, cache=self.cache, **endpoint)
        )
        self.cache.install(app)
        app.add_middleware(
            ServerSessionMiddleware, cookie_name="session", secure=False, users=self.sessions
        )
        self.app = app

    def browser(self, user: str, sid: str) -> TestClient:
        client = TestClient(self.app)
        client.post("/fake-login", params={"user": user, "sid": sid})
        assert client.get("/me").json() == {"user": user}
        return client

    def logout(self, token: str) -> httpx.Response:
        return TestClient(self.app).post("/backchannel-logout", data={"logout_token": token})


class TestBackChannelLogout:
    async def test_ends_the_sessions_of_that_login(self) -> None:
        provider = LogoutProvider()
        app = Browser(await discovered(provider))
        first, second = app.browser("user-1", "sid-a"), app.browser("user-1", "sid-b")
        other = app.browser("user-2", "sid-c")
        response = app.logout(provider.logout_token(sid="sid-a"))
        assert (response.status_code, response.headers["cache-control"]) == (200, "no-store")
        assert first.get("/me").json() == {"user": None}
        assert second.get("/me").json() == {"user": "user-1"}
        assert other.get("/me").json() == {"user": "user-2"}

    async def test_a_sub_alone_ends_every_session_of_the_user(self) -> None:
        provider = LogoutProvider()
        app = Browser(await discovered(provider), user_id=lambda sub: sub)
        first, second = app.browser("user-1", "sid-a"), app.browser("user-1", "sid-b")
        assert app.logout(provider.logout_token(sid=None)).status_code == 200
        assert first.get("/me").json() == second.get("/me").json() == {"user": None}

    async def test_a_sid_no_session_has_ends_nothing(self) -> None:
        """The user's other logins aren't that one: a sid names one."""
        provider = LogoutProvider()

        async def user_of(sub: str) -> str:
            return sub

        app = Browser(await discovered(provider), user_id=user_of)
        first = app.browser("user-1", "sid-a")
        assert app.logout(provider.logout_token(sid="sid-unknown")).status_code == 200
        assert first.get("/me").json() == {"user": "user-1"}
        assert app.logout(provider.logout_token(sid=None)).status_code == 200  # async user_id
        assert first.get("/me").json() == {"user": None}

    async def test_a_sub_the_app_doesnt_know(self) -> None:
        provider = LogoutProvider()
        app = Browser(await discovered(provider), user_id=lambda sub: None)
        first = app.browser("user-1", "sid-a")
        assert app.logout(provider.logout_token(sid=None)).status_code == 200
        assert first.get("/me").json() == {"user": "user-1"}

    async def test_refused_replayed_and_malformed(self, caplog: pytest.LogCaptureFixture) -> None:
        provider = LogoutProvider()
        app = Browser(await discovered(provider))
        first = app.browser("user-1", "sid-a")
        token = provider.logout_token(sid="sid-b")
        assert app.logout(token).status_code == 200
        replay = app.logout(token)
        assert (replay.status_code, replay.json()) == (400, {"error": "invalid_request"})
        assert app.logout(provider.logout_token(nonce="n")).status_code == 400
        assert TestClient(app.app).post("/backchannel-logout", data={}).status_code == 400
        assert first.get("/me").json() == {"user": "user-1"}
        assert "used already" in caplog.text

    async def test_sealed_sessions_and_an_unknown_sid(self) -> None:
        provider = LogoutProvider()
        app = Browser(await discovered(provider), signing_key="k" * 32)
        first = app.browser("user-1", "sid-a")
        assert await app.sessions.end_sid("sid-nobody-has") == 0
        assert app.logout(provider.logout_token(sid="sid-a")).status_code == 200
        assert first.get("/me").json() == {"user": None}
        assert await app.sessions.end_sid("sid-a") == 0  # ended already
