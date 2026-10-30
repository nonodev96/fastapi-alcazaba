"""The JWT example: a password gives a token, which scripts send until it expires or is revoked."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from examples.auth_strategies import jwt_tokens
from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.auth.strategies.jwt import JWTStrategy

OCTOCAT = {"id": "1", "login": "octocat", "name": "The Octocat"}
DEMO = {"login": "octocat", "password": jwt_tokens.DEMO_PASSWORD}


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(jwt_tokens, "db", jwt_tokens.Database())
    with TestClient(jwt_tokens.app) as client:
        yield client
    FastAPICache.reset()


def log_in(client: TestClient) -> dict[str, str]:
    response = client.post("/token", json=DEMO)
    assert response.status_code == 200
    assert response.json()["expires_in"] == 3600
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


class TestJWTExample:
    def test_nobody_without_a_token(self, client: TestClient) -> None:
        refused = client.get("/me")
        assert (refused.status_code, refused.headers["www-authenticate"]) == (401, "Bearer")

    def test_a_wrong_password(self, client: TestClient) -> None:
        assert client.post("/token", json=DEMO | {"password": "guess"}).status_code == 401
        assert client.post("/token", json=DEMO | {"login": "nobody"}).status_code == 401

    def test_scripts_send_the_token(self, client: TestClient) -> None:
        assert client.get("/me", headers=log_in(client)).json() == OCTOCAT

    def test_tokens_for_another_api_are_refused(self, client: TestClient) -> None:
        other = JWTStrategy(jwt_tokens.settings.jwt_secret, audience="https://other.example.com")
        token = other.issue("1")
        assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_revoking_the_token(self, client: TestClient) -> None:
        token = log_in(client)
        assert client.delete("/token", headers=token).status_code == 204
        assert client.get("/me", headers=token).status_code == 401

    def test_guessing_passwords_gets_429s(self, client: TestClient) -> None:
        wrong = DEMO | {"password": "guess"}
        statuses = [client.post("/token", json=wrong).status_code for _ in range(6)]
        assert statuses == [401] * 5 + [429]
