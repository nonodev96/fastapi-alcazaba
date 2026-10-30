"""The authz example: a newsroom whose Casbin policy says who reads, writes, publishes and grants
roles."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from examples.authz import main
from examples.authz.main import app

DRAFT = {"title": "Mill opens", "body": "The new mill opens on Monday."}


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(main, "db", main.Database())
    with TestClient(app) as client:  # the lifespan loads the policy again: no roles granted
        yield client


def token(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user}-token"}


def titles(client: TestClient, user: str | None = None) -> list[str]:
    response = client.get("/articles", headers=token(user) if user else None)
    assert response.status_code == 200
    return [article["title"] for article in response.json()]


class TestReading:
    def test_visitors_read_the_published_articles(self, client: TestClient) -> None:
        assert titles(client) == ["Olive harvest starts"]
        assert client.get("/articles/1").status_code == 200
        assert client.get("/articles/2").status_code == 404  # a draft

    def test_users_without_a_role_read_what_visitors_read(self, client: TestClient) -> None:
        assert titles(client, "dan") == ["Olive harvest starts"]
        assert client.get("/articles/2", headers=token("dan")).status_code == 404

    def test_writers_read_the_drafts(self, client: TestClient) -> None:
        assert titles(client, "cris") == ["Olive harvest starts", "Draft: new mill"]
        assert client.get("/articles/2", headers=token("ben")).status_code == 200

    def test_a_wrong_token_is_not_a_visitor(self, client: TestClient) -> None:
        refused = client.get("/articles", headers={"Authorization": "Bearer nonsense"})
        assert (refused.status_code, refused.json()) == (401, {"detail": "Invalid token"})


class TestWriting:
    def test_writers_write(self, client: TestClient) -> None:
        written = client.post("/articles", json=DRAFT, headers=token("cris"))
        assert written.status_code == 201
        assert written.json() == {"id": 3, "author": "cris", "published_by": None, **DRAFT}

    def test_visitors_must_log_in(self, client: TestClient) -> None:
        refused = client.post("/articles", json=DRAFT)
        assert (refused.status_code, refused.json()) == (401, {"detail": "Not authenticated"})
        assert refused.headers["www-authenticate"] == "Bearer"

    def test_users_without_the_role_are_refused(self, client: TestClient) -> None:
        refused = client.post("/articles", json=DRAFT, headers=token("dan"))
        assert (refused.status_code, refused.json()) == (403, {"detail": "Forbidden"})

    def test_authors_and_editors_edit(self, client: TestClient) -> None:
        assert client.put("/articles/2", json=DRAFT, headers=token("cris")).status_code == 200
        assert client.put("/articles/2", json=DRAFT, headers=token("ben")).status_code == 200
        client.post("/articles", json=DRAFT, headers=token("ben"))
        refused = client.put("/articles/3", json=DRAFT, headers=token("cris"))
        assert refused.status_code == 403  # a writer, not its author


class TestPublishing:
    def test_editors_publish(self, client: TestClient) -> None:
        published = client.post("/articles/2/publish", headers=token("ben"))
        assert published.json()["published_by"] == "ben"
        assert titles(client) == ["Olive harvest starts", "Draft: new mill"]

    def test_writers_dont(self, client: TestClient) -> None:
        assert client.post("/articles/2/publish", headers=token("cris")).status_code == 403
        assert client.post("/articles/2/publish").status_code == 401


class TestRoles:
    def test_admins_grant_roles_that_take_effect_at_once(self, client: TestClient) -> None:
        assert client.post("/articles", json=DRAFT, headers=token("dan")).status_code == 403
        granted = client.put("/users/dan/roles/writer", headers=token("ana"))
        assert granted.status_code == 204
        assert client.post("/articles", json=DRAFT, headers=token("dan")).status_code == 201

    def test_only_admins(self, client: TestClient) -> None:
        assert client.put("/users/dan/roles/admin", headers=token("ben")).status_code == 403

    def test_only_the_roles_the_policy_knows(self, client: TestClient) -> None:
        unknown = client.put("/users/dan/roles/root", headers=token("ana"))
        assert unknown.status_code == 422
