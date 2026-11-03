"""OIDCStrategy against a real OpenID Connect provider (Keycloak), enabled by its variables:

    OIDC_LIVE_ISSUER=http://localhost:18090/realms/alcazaba
    OIDC_LIVE_CLIENT_ID=alcazaba-example     OIDC_LIVE_CLIENT_SECRET=...
    OIDC_LIVE_USERNAME=ana                   OIDC_LIVE_PASSWORD=...

The realm's client is confidential, with the standard flow, the redirect URI
http://localhost:8006/auth/callback and the post-logout redirect URI
http://localhost:8006/logged-out; its user has a verified e-mail address:
tests/integration/keycloak/alcazaba-realm.json, which CI imports. The test plays the browser: the
app's redirect, Keycloak's login form, the callback, the logout.
"""

import html
import os
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from fastapi_alcazaba import FastAPIAuth, FastAPICache
from fastapi_alcazaba.auth import SessionStrategy
from fastapi_alcazaba.auth.strategies.oidc import OIDCStrategy
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.security import ServerSessionMiddleware

pytestmark = pytest.mark.integration

VARIABLES = ["ISSUER", "CLIENT_ID", "CLIENT_SECRET", "USERNAME", "PASSWORD"]
REDIRECT_URI = "http://localhost:8006/auth/callback"
LOGGED_OUT = "http://localhost:8006/logged-out"


@pytest.fixture(name="provider", scope="module")
def fixture_provider() -> dict[str, str]:
    found = {name: os.environ.get(f"OIDC_LIVE_{name}") for name in VARIABLES}
    if not all(found.values()):
        pytest.skip("OIDC_LIVE_* not set")
    return {name.lower(): value for name, value in found.items() if value is not None}


@pytest.fixture(name="app")
def fixture_app(provider: dict[str, str]) -> Iterator[TestClient]:
    auth = FastAPIAuth()
    oidc = auth.use(
        OIDCStrategy(
            name="oidc",
            issuer=provider["issuer"],
            client_id=provider["client_id"],
            client_secret=provider["client_secret"],
            redirect_uri=REDIRECT_URI,
            userinfo=True,
            post_logout_redirect_uris=[LOGGED_OUT],
        )
    )
    sessions = auth.use(SessionStrategy(lambda user_id: {"id": user_id}))
    app = FastAPI()

    @app.get("/login")
    async def login() -> Any:
        return oidc.redirect()

    @app.get("/auth/callback")
    async def callback(request: Request) -> dict[str, Any]:
        user = await auth.user(request, "oidc")
        if user is None:
            return {"refused": True}
        sessions.login(request, user["sub"])
        return {
            "sub": user["sub"],
            "email": user.get("email"),
            "iss": user["iss"],
            "sid": user.get("sid"),
            "id_token": request.state.id_token,
        }

    @app.post("/logout")
    async def logout(request: Request) -> dict[str, str]:
        body = await request.json()
        url = oidc.logout_url(
            id_token_hint=body["id_token"], post_logout_redirect_uri=LOGGED_OUT, state="bye"
        )
        return {"url": url}

    import asyncio

    asyncio.run(oidc.discover())
    FastAPICache.init(InMemoryBackend(), signing_key="s" * 32)
    app.add_middleware(ServerSessionMiddleware)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        yield client
    FastAPICache.reset()


def through_keycloak(
    app: TestClient,
    provider: dict[str, str],
    jar: dict[str, str] | None = None,
) -> str:
    """The app's redirect, Keycloak's login form: the callback's URL, with its code. `jar` keeps
    Keycloak's cookies, the browser's."""
    location = app.get("/login").headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert {"nonce", "state", "code_challenge"} <= set(query)
    jar = jar if jar is not None else {}

    def keycloak(method: str, url: str, **options: Any) -> httpx.Response:
        cookies = "; ".join(f"{name}={value}" for name, value in jar.items())
        response = httpx.request(method, url, headers={"Cookie": cookies}, **options)
        for header in response.headers.get_list("set-cookie"):
            name, _, rest = header.partition("=")
            jar[name] = rest.split(";")[0]
        return response

    page = keycloak("GET", location)
    found = re.search(r'id="kc-form-login"[^>]*action="([^"]+)"', page.text)
    assert found is not None, page.text[:500]
    credentials = {"username": provider["username"], "password": provider["password"]}
    back = keycloak("POST", html.unescape(found.group(1)), data=credentials)
    assert back.status_code == 302, back.text[:500]
    callback: str = back.headers["location"]
    assert callback.startswith(REDIRECT_URI)
    return callback


def test_a_login(app: TestClient, provider: dict[str, str]) -> None:
    callback = through_keycloak(app, provider)
    user = app.get(urlsplit(callback)._replace(scheme="", netloc="").geturl()).json()
    assert user["iss"] == provider["issuer"]
    assert user["sub"]
    assert user["email"]


def test_a_code_used_twice(app: TestClient, provider: dict[str, str]) -> None:
    path = urlsplit(through_keycloak(app, provider))._replace(scheme="", netloc="").geturl()
    assert "sub" in app.get(path).json()
    assert app.get(path).json() == {"refused": True}  # Keycloak refuses a code used already


def test_a_callback_from_another_browser(app: TestClient, provider: dict[str, str]) -> None:
    path = urlsplit(through_keycloak(app, provider))._replace(scheme="", netloc="").geturl()
    other = TestClient(app.app, base_url="https://testserver")
    assert other.get(path).json() == {"refused": True}  # without the state cookie


def test_logging_out_at_keycloak(app: TestClient, provider: dict[str, str]) -> None:
    """RP-Initiated Logout: with the ID token as a hint, Keycloak ends its session without asking,
    and sends the browser back to the registered URI with the state."""
    jar: dict[str, str] = {}
    path = urlsplit(through_keycloak(app, provider, jar))._replace(scheme="", netloc="").geturl()
    login = app.get(path).json()
    assert login["sid"]
    url = app.post("/logout", json={"id_token": login["id_token"]}).json()["url"]
    assert url.startswith(f"{provider['issuer']}/protocol/openid-connect/logout?")
    cookies = "; ".join(f"{name}={value}" for name, value in jar.items())
    back = httpx.get(url, headers={"Cookie": cookies})
    assert back.status_code == 302, back.text[:500]
    assert back.headers["location"] == f"{LOGGED_OUT}?state=bye"
    # Keycloak's session is over: the login page asks for the password again.
    again = httpx.get(app.get("/login").headers["location"], headers={"Cookie": cookies})
    assert 'id="kc-form-login"' in again.text
