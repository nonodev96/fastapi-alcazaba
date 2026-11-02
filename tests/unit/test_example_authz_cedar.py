"""The Cedar example: a team's documents, read, edited and deleted as their attributes allow."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from examples.authz import cedar_policies
from examples.authz.cedar_policies import app
from fastapi_alcazaba.authz.cedar import CedarEnforcer


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(cedar_policies, "db", cedar_policies.Database())
    with TestClient(app) as client:
        yield client


def token(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user}-token"}


def titles(client: TestClient, user: str) -> list[str]:
    response = client.get("/documents", headers=token(user))
    assert response.status_code == 200
    return [document["title"] for document in response.json()]


class TestCedarExample:
    def test_each_team_reads_its_documents(self, client: TestClient) -> None:
        assert titles(client, "ben") == ["Style guide", "Budget 2027"]
        assert titles(client, "dan") == ["Sales targets"]
        assert client.get("/documents/3", headers=token("ben")).status_code == 404

    def test_contractors_never_see_confidential_documents(self, client: TestClient) -> None:
        assert titles(client, "eva") == ["Style guide"]
        assert client.get("/documents/2", headers=token("eva")).status_code == 404

    def test_only_the_owner_edits(self, client: TestClient) -> None:
        edit = {"title": "Budget 2027, second draft"}
        assert client.put("/documents/2", json=edit, headers=token("ben")).json()["title"] == (
            "Budget 2027, second draft"
        )
        refused = client.put("/documents/2", json=edit, headers=token("ana"))
        assert (refused.status_code, refused.json()) == (
            403,
            {"detail": "Only its owner edits a document"},
        )

    def test_admins_delete(self, client: TestClient) -> None:
        assert client.delete("/documents/3", headers=token("dan")).status_code == 403
        assert client.delete("/documents/3", headers=token("ana")).status_code == 204
        assert titles(client, "dan") == []

    def test_without_a_token(self, client: TestClient) -> None:
        for response in (client.get("/documents"), client.get("/documents/1")):
            assert response.status_code == 401
        assert client.get("/documents", headers=token("nobody")).status_code == 401

    def test_the_policies_are_checked_against_the_schema(self) -> None:
        policies = (cedar_policies.HERE / "policies.cedar").read_text()
        schema = (cedar_policies.HERE / "schema.cedarschema").read_text()
        with pytest.raises(ValueError, match="don't fit the schema"):
            CedarEnforcer(policies.replace("principal.team", "principal.teem"), schema=schema)
