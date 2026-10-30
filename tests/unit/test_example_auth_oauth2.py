"""The OAuth2 example: logging in with GitHub, then a session."""

from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from examples.auth_strategies import oauth2_login
from fastapi_alcazaba import FastAPICache

SAME_SITE = {"Sec-Fetch-Site": "same-origin"}  # what browsers send with the app's own requests
OCTOCAT = {"id": "github:583231", "login": "octocat", "name": "The Octocat"}


def github(request: httpx.Request) -> httpx.Response:
    """Stands in for GitHub's token and user endpoints."""
    if request.url.path == "/login/oauth/access_token":
        form = parse_qs(request.content.decode())
        if form["code"] != ["the-code"] or not form.get("code_verifier"):
            return httpx.Response(200, json={"error": "bad_verification_code"})
        return httpx.Response(200, json={"access_token": "gho_123", "token_type": "bearer"})
    assert request.headers["authorization"] == "Bearer gho_123"
    return httpx.Response(200, json={"id": 583231, "login": "octocat", "name": "The Octocat"})


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(oauth2_login, "db", oauth2_login.Database())
    monkeypatch.setattr(
        oauth2_login.github, "client", httpx.AsyncClient(transport=httpx.MockTransport(github))
    )
    # https: the cookies are Secure.
    with TestClient(
        oauth2_login.app, base_url="https://testserver", follow_redirects=False
    ) as client:
        yield client
    FastAPICache.reset()


def log_in(client: TestClient, code: str = "the-code", state: str | None = None) -> httpx.Response:
    """Through GitHub: the redirect to its login page, then its redirect back to the callback."""
    to_github = client.get("/login/github")
    state = state or parse_qs(urlsplit(to_github.headers["location"]).query)["state"][0]
    return client.get("/auth/github/callback", params={"code": code, "state": state})


class TestOAuth2Example:
    def test_the_redirect_to_github(self, client: TestClient) -> None:
        redirect = client.get("/login/github")
        location = urlsplit(redirect.headers["location"])
        assert (location.netloc, location.path) == ("github.com", "/login/oauth/authorize")
        query = parse_qs(location.query)
        assert query["redirect_uri"] == ["http://localhost:8000/auth/github/callback"]
        assert query["code_challenge_method"] == ["S256"]

    def test_logging_in_starts_a_session(self, client: TestClient) -> None:
        assert client.get("/me").status_code == 401
        callback = log_in(client)
        assert (callback.status_code, callback.headers["location"]) == (303, "/me")
        assert client.get("/me").json() == OCTOCAT
        log_in(client)  # again: the same user
        assert list(oauth2_login.db.users) == ["github:583231"]

    def test_a_code_github_refuses(self, client: TestClient) -> None:
        assert log_in(client, code="stolen").status_code == 401
        assert client.get("/me").status_code == 401

    def test_a_callback_with_another_state(self, client: TestClient) -> None:
        """Someone else's link to the callback: refused, it doesn't log you in as them."""
        assert log_in(client, state="their-state").status_code == 401

    def test_logging_out(self, client: TestClient) -> None:
        log_in(client)
        assert client.post("/logout", headers=SAME_SITE).status_code == 204
        assert client.get("/me").status_code == 401
