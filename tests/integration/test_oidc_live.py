"""OIDCStrategy against a real OpenID Connect provider (Keycloak), enabled by its variables:

    OIDC_LIVE_ISSUER=http://localhost:18090/realms/alcazaba
    OIDC_LIVE_CLIENT_ID=alcazaba-example     OIDC_LIVE_CLIENT_SECRET=...
    OIDC_LIVE_USERNAME=ana                   OIDC_LIVE_PASSWORD=...

The realm's client is confidential, with the standard flow and the redirect URI
http://localhost:8006/auth/callback; its user has a verified e-mail address. The test plays the
browser: the app's redirect, Keycloak's login form, the callback.
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
        return {"sub": user["sub"], "email": user.get("email"), "iss": user["iss"]}

    import asyncio

    asyncio.run(oidc.discover())
    FastAPICache.init(InMemoryBackend(), signing_key="s" * 32)
    app.add_middleware(ServerSessionMiddleware)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        yield client
    FastAPICache.reset()


def through_keycloak(app: TestClient, provider: dict[str, str]) -> str:
    """The app's redirect, Keycloak's login form: the callback's URL, with its code."""
    location = app.get("/login").headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert {"nonce", "state", "code_challenge"} <= set(query)
    jar: dict[str, str] = {}

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
