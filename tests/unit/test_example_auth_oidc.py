"""The OpenID Connect example: the ID token verified, then a session, against a fake provider."""

# pylint: disable=protected-access  # the strategy's kept keys, reset for each test

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from examples.auth_strategies import oidc_login
from fastapi_alcazaba import FastAPICache
from tests.unit.test_auth_oidc import Provider

ISSUER = oidc_login.settings.oidc_issuer
SAME_SITE = {"Sec-Fetch-Site": "same-origin"}  # what browsers send with the app's own requests


class ExampleProvider(Provider):
    """The fake provider, at the example's issuer and for its client."""

    def sign(self, **claims: Any) -> str:
        defaults = {"iss": ISSUER, "aud": oidc_login.settings.oidc_client_id, "name": "Ana"}
        # The test's claims (self.claims) still win over the example's defaults.
        return super().sign(**{**defaults, "email_verified": True, **self.claims, **claims})

    def handler(self, request: httpx.Request) -> httpx.Response:
        # The fake's endpoints are under its own issuer: the example's paths, there.
        url = str(request.url).replace(ISSUER, "https://idp.example.com")
        response = super().handler(
            httpx.Request(request.method, url, headers=request.headers, content=request.content)
        )
        if request.url.path.endswith("/.well-known/openid-configuration"):
            document = {
                key: value.replace("https://idp.example.com", ISSUER)
                for key, value in response.json().items()
            }
            return httpx.Response(200, json=document)
        return response


@pytest.fixture(name="provider")
def fixture_provider() -> Provider:
    return ExampleProvider()


@pytest.fixture(name="client")
async def fixture_client(
    provider: Provider,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[TestClient]:
    monkeypatch.setattr(oidc_login, "db", oidc_login.Database())
    transport = httpx.MockTransport(provider.handler)
    monkeypatch.setattr(oidc_login.provider, "client", httpx.AsyncClient(transport=transport))
    for endpoint in ("authorization_url", "token_url", "userinfo_url", "jwks_url"):
        monkeypatch.setattr(oidc_login.provider, endpoint, None)  # discovered at each start
    # Each test's provider has keys of its own, with the same kid: none kept from the last one.
    monkeypatch.setattr(oidc_login.provider, "_keys", {})
    monkeypatch.setattr(oidc_login.provider, "_keys_fetched", -float("inf"))
    app = oidc_login.app
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        yield client
    FastAPICache.reset()


def log_in(client: TestClient, provider: Provider) -> httpx.Response:
    location = client.get("/login").headers["location"]
    query = parse_qs(urlsplit(location).query)
    provider.nonce = query["nonce"][0]
    return client.get("/auth/callback", params={"code": "the-code", "state": query["state"][0]})


def test_the_endpoints_are_discovered_at_start(client: TestClient) -> None:
    location = urlsplit(client.get("/login").headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/authorize"
    assert parse_qs(location.query)["scope"] == ["openid email profile"]


def test_logging_in_starts_a_session(client: TestClient, provider: Provider) -> None:
    assert client.get("/me").status_code == 401
    callback = log_in(client, provider)
    assert (callback.status_code, callback.headers["location"]) == (303, "/me")
    user_id = f"{ISSUER}#user-1"
    assert client.get("/me").json() == {"id": user_id, "email": "ana@example.com", "name": "Ana"}
    log_in(client, provider)  # again: the same user
    assert list(oidc_login.db.users) == [user_id]


def test_an_address_nobody_proved_is_refused(client: TestClient, provider: Provider) -> None:
    provider.claims = {"email_verified": False}
    assert log_in(client, provider).status_code == 401


def test_a_token_for_another_app_is_refused(client: TestClient, provider: Provider) -> None:
    provider.claims = {"aud": "another-app"}
    assert log_in(client, provider).status_code == 401
    assert client.get("/me").status_code == 401


def test_logging_out(client: TestClient, provider: Provider) -> None:
    log_in(client, provider)
    assert client.post("/logout", headers=SAME_SITE).status_code == 204
    assert client.get("/me").status_code == 401
