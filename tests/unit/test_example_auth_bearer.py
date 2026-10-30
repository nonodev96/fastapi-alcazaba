"""The bearer tokens example: random tokens kept as digests, refused as soon as they're deleted."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from examples.auth_strategies import bearer_tokens
from fastapi_alcazaba import FastAPICache

OCTOCAT = {"id": "1", "login": "octocat", "name": "The Octocat"}
DEMO = {"login": "octocat", "password": bearer_tokens.DEMO_PASSWORD}


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(bearer_tokens, "db", bearer_tokens.Database())
    with TestClient(bearer_tokens.app) as client:
        yield client
    FastAPICache.reset()


def new_token(client: TestClient) -> str:
    created = client.post("/tokens", json=DEMO)
    assert created.status_code == 201
    token: str = created.json()["token"]
    return token


class TestBearerExample:
    def test_nobody_without_a_token(self, client: TestClient) -> None:
        refused = client.get("/me")
        assert (refused.status_code, refused.headers["www-authenticate"]) == (401, "Bearer")

    def test_scripts_send_the_token(self, client: TestClient) -> None:
        token = new_token(client)
        assert token.startswith("pat_")
        assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).json() == OCTOCAT

    def test_only_digests_are_kept(self, client: TestClient) -> None:
        token = new_token(client)
        assert token not in bearer_tokens.db.tokens
        assert list(bearer_tokens.db.tokens) == [bearer_tokens.token_digest(token)]

    def test_unknown_tokens_are_refused(self, client: TestClient) -> None:
        unknown = {"Authorization": "Bearer pat_made-up"}
        assert client.get("/me", headers=unknown).status_code == 401

    def test_a_deleted_token_is_refused_at_once(self, client: TestClient) -> None:
        token = {"Authorization": f"Bearer {new_token(client)}"}
        assert client.delete("/tokens/current", headers=token).status_code == 204
        assert client.get("/me", headers=token).status_code == 401

    def test_a_wrong_password(self, client: TestClient) -> None:
        assert client.post("/tokens", json=DEMO | {"password": "guess"}).status_code == 401
