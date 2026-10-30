"""The example of a strategy of your own: API keys in X-API-Key, checked by FastAPIAPIKeys."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from examples.auth_strategies import api_key_strategy
from fastapi_alcazaba import FastAPICache

OCTOCAT = {"id": "1", "login": "octocat", "name": "The Octocat"}
DEMO = {"login": "octocat", "password": api_key_strategy.DEMO_PASSWORD}


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(api_key_strategy, "db", api_key_strategy.Database())
    api_key_strategy.keys.forget()
    with TestClient(api_key_strategy.app) as client:
        yield client
    FastAPICache.reset()


def new_key(client: TestClient) -> str:
    created = client.post("/api-keys", json=DEMO)
    assert created.status_code == 201
    key: str = created.json()["key"]
    return key


class TestAPIKeyStrategyExample:
    def test_nobody_without_a_key(self, client: TestClient) -> None:
        refused = client.get("/me")
        assert (refused.status_code, refused.headers["www-authenticate"]) == (401, "APIKey")

    def test_programs_send_the_key(self, client: TestClient) -> None:
        key = new_key(client)
        assert key.startswith("demo_")
        assert client.get("/me", headers={"X-API-Key": key}).json() == OCTOCAT

    def test_a_wrong_key_is_refused(self, client: TestClient) -> None:
        key = new_key(client)
        forged = key[:-4] + ("AAAA" if not key.endswith("AAAA") else "BBBB")
        assert client.get("/me", headers={"X-API-Key": forged}).status_code == 401
        assert client.get("/me", headers={"X-API-Key": "not a key"}).status_code == 401

    def test_a_key_whose_owner_is_gone(self, client: TestClient) -> None:
        key = new_key(client)
        api_key_strategy.db.users.clear()
        assert client.get("/me", headers={"X-API-Key": key}).status_code == 401

    def test_the_docs_authorize_button(self, client: TestClient) -> None:
        schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]
        assert schemes["api-key"] == {"type": "apiKey", "in": "header", "name": "X-API-Key"}
