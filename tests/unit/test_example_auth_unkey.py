"""The Unkey example: keys made at sign-up, verified with their permissions, credits and rate
limit, and deleted, against a fake of Unkey's API that keeps them."""

import json
import secrets
from collections.abc import Iterator
from typing import Any

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from examples.auth_strategies import unkey_keys


class FakeUnkey:
    """Stands in for Unkey: its keys, their credits and their requests of the minute."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []  # (operation, root key)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        operation = request.url.path.removeprefix("/v2/")
        self.calls.append((operation, request.headers["Authorization"].removeprefix("Bearer ")))
        body = json.loads(request.content)
        operations = {
            "keys.createKey": self.create_key,
            "keys.deleteKey": self.delete_key,
            "keys.verifyKey": self.verify_key,
        }
        data = operations[operation](body)
        return httpx.Response(200, json={"meta": {"requestId": "req_1"}, "data": data})

    def create_key(self, body: dict[str, Any]) -> dict[str, Any]:
        key = f"{body['prefix']}_{secrets.token_hex(8)}"
        self.keys[key] = {
            "keyId": f"key_{len(self.keys) + 1}",
            "externalId": body["externalId"],
            "permissions": set(body["permissions"]),
            "credits": body["credits"]["remaining"],
            "limit": body["ratelimits"][0]["limit"],
            "requests": 0,
        }
        return {"key": key, "keyId": self.keys[key]["keyId"]}

    def delete_key(self, body: dict[str, Any]) -> dict[str, Any]:
        self.keys = {k: v for k, v in self.keys.items() if v["keyId"] != body["keyId"]}
        return {}

    def verify_key(self, body: dict[str, Any]) -> dict[str, Any]:
        found = self.keys.get(body["key"])
        if found is None:
            return {"valid": False, "code": "NOT_FOUND"}
        answer: dict[str, Any] = {"keyId": found["keyId"]}
        if body.get("permissions") not in found["permissions"]:
            return {**answer, "valid": False, "code": "INSUFFICIENT_PERMISSIONS"}
        found["requests"] += 1
        if found["requests"] > found["limit"]:
            limit = {
                "id": "rl_1",
                "name": "requests",
                "limit": found["limit"],
                "duration": 60_000,
                "reset": 30_000,
                "remaining": 0,
                "exceeded": True,
                "autoApply": True,
            }
            return {**answer, "valid": False, "code": "RATE_LIMITED", "ratelimits": [limit]}
        cost = body["credits"]["cost"]
        if found["credits"] < cost:
            return {**answer, "valid": False, "code": "USAGE_EXCEEDED", "credits": 0}
        found["credits"] -= cost
        identity = {"id": "id_1", "externalId": found["externalId"]}
        valid = {"valid": True, "code": "VALID", "identity": identity, "credits": found["credits"]}
        return {**answer, **valid}


@pytest.fixture(name="unkey")
def fixture_unkey(monkeypatch: pytest.MonkeyPatch) -> FakeUnkey:
    fake = FakeUnkey()
    monkeypatch.setattr(unkey_keys, "db", unkey_keys.Database())
    transport = httpx.MockTransport(fake)
    for client in (unkey_keys.verifier, unkey_keys.admin):
        monkeypatch.setattr(client, "client", httpx.AsyncClient(transport=transport))
    return fake


@pytest.fixture(name="client")
def fixture_client(unkey: FakeUnkey) -> Iterator[TestClient]:
    del unkey  # in place before the app starts
    with TestClient(unkey_keys.app) as client:
        yield client


def sign_up(client: TestClient, username: str = "ana") -> dict[str, str]:
    created = client.post("/signup", json={"username": username})
    assert created.status_code == 201
    new: dict[str, str] = created.json()
    return new


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_a_key_at_sign_up(client: TestClient, unkey: FakeUnkey) -> None:
    new = sign_up(client)
    assert new["key"].startswith("docs_")
    documents = client.get("/documents", headers=bearer(new["key"]))
    assert documents.json() == [{"title": "Style guide", "owner": "unkey"}]
    # Each root key for its own: the admin one creates, the verify one verifies.
    assert unkey.calls == [
        ("keys.createKey", "set UNKEY_ADMIN_KEY"),
        ("keys.verifyKey", "set UNKEY_VERIFY_KEY"),
    ]


def test_writing_needs_its_permission(client: TestClient, unkey: FakeUnkey) -> None:
    new = sign_up(client)
    refused = client.post("/documents", json={"title": "Draft"}, headers=bearer(new["key"]))
    assert (refused.status_code, refused.json()) == (403, {"detail": "Insufficient permissions"})
    next(iter(unkey.keys.values()))["permissions"].add("documents.write")
    written = client.post("/documents", json={"title": "Draft"}, headers=bearer(new["key"]))
    assert written.json() == {"title": "Draft", "owner": "ana"}
    assert next(iter(unkey.keys.values()))["credits"] == 95  # five credits


def test_the_rate_limit(client: TestClient) -> None:
    new = sign_up(client)
    statuses = [client.get("/documents", headers=bearer(new["key"])).status_code for _ in range(11)]
    assert statuses == [200] * 10 + [429]
    refused = client.get("/documents", headers=bearer(new["key"]))
    assert refused.headers["Retry-After"] == "30"


def test_out_of_credits(client: TestClient, unkey: FakeUnkey) -> None:
    new = sign_up(client)
    next(iter(unkey.keys.values()))["credits"] = 0
    refused = client.get("/documents", headers=bearer(new["key"]))
    assert (refused.status_code, refused.json()) == (429, {"detail": "No credits left"})


def test_deleting_a_key(client: TestClient) -> None:
    ana, ben = sign_up(client, "ana"), sign_up(client, "ben")
    assert client.delete(f"/keys/{ben['key_id']}", headers=bearer(ana["key"])).status_code == 404
    assert client.delete(f"/keys/{ana['key_id']}", headers=bearer(ana["key"])).status_code == 204
    refused = client.get("/documents", headers=bearer(ana["key"]))
    assert (refused.status_code, refused.headers["WWW-Authenticate"]) == (401, "Bearer")
    assert client.get("/documents", headers=bearer(ben["key"])).status_code == 200


def test_sign_up(client: TestClient) -> None:
    sign_up(client)
    assert client.post("/signup", json={"username": "ana"}).status_code == 409
    assert client.post("/signup", json={"username": "Ana!"}).status_code == 422
    assert client.get("/documents").status_code == 401
