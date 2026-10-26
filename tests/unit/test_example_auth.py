"""The auth example: login codes by e-mail, access tokens that can be revoked, and API keys."""

import re
import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from examples.auth import main
from examples.auth.main import TOKEN_LIFETIME, app
from fastapi_alcazaba import FastAPICache

ANA = "ana@example.com"
BEN = "ben@example.com"


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(main, "db", main.Database())
    monkeypatch.setattr(main, "outbox", [])
    with TestClient(app) as client:
        yield client
    FastAPICache.reset()


class Clock:
    """Moves time.time forward, which the tokens and the blocklist read."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.offset = 0.0
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + self.offset)

    def advance(self, seconds: float) -> None:
        self.offset += seconds


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


def last_code(email: str) -> str:
    to, text = main.outbox[-1]
    assert to == email
    found = re.search(r"\b\d{6}\b", text)
    assert found is not None
    return found.group()


def login(client: TestClient, email: str = ANA) -> dict[str, str]:
    """The Authorization header of a new access token, got through an e-mailed code."""
    assert client.post("/login/code", json={"email": email}).status_code == 202
    response = client.post("/login", json={"email": email, "code": last_code(email)})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def new_key(client: TestClient, auth: dict[str, str], *scopes: str) -> dict[str, str]:
    """The X-API-Key header of a new key with `scopes`."""
    response = client.post("/api-keys", json={"scopes": scopes}, headers=auth)
    assert response.status_code == 201
    return {"X-API-Key": response.json()["key"]}


def accepted(client: TestClient, auth: dict[str, str]) -> bool:
    """Whether a request with this access token gets through."""
    response = client.post("/api-keys", json={"scopes": ["orders:read"]}, headers=auth)
    assert response.status_code in (201, 401)
    return response.status_code == 201


class TestLoginCodes:
    def test_the_code_is_emailed_never_returned(self, client: TestClient) -> None:
        response = client.post("/login/code", json={"email": "Ana@Example.com"})
        assert response.status_code == 202
        code = last_code(ANA)
        assert code not in response.text
        token = client.post("/login", json={"email": ANA, "code": code}).json()
        assert token["expires_in"] == TOKEN_LIFETIME
        assert accepted(client, {"Authorization": f"Bearer {token['access_token']}"})

    def test_a_code_works_once(self, client: TestClient) -> None:
        client.post("/login/code", json={"email": ANA})
        form = {"email": ANA, "code": last_code(ANA)}
        assert client.post("/login", json=form).status_code == 200
        again = client.post("/login", json=form)
        assert (again.status_code, again.json()) == (400, {"detail": "Invalid or expired code"})

    def test_a_new_code_replaces_the_previous_one(self, client: TestClient) -> None:
        client.post("/login/code", json={"email": ANA})
        first = last_code(ANA)
        client.post("/login/code", json={"email": ANA})
        if last_code(ANA) != first:  # one chance in a million of the same code
            assert client.post("/login", json={"email": ANA, "code": first}).status_code == 400
        assert client.post("/login", json={"email": ANA, "code": last_code(ANA)}).status_code == 200

    def test_a_code_is_lost_after_five_guesses(self, client: TestClient) -> None:
        client.post("/login/code", json={"email": ANA})
        code = last_code(ANA)
        wrong = f"{(int(code) + 1) % 1_000_000:06d}"
        for _ in range(5):
            assert client.post("/login", json={"email": ANA, "code": wrong}).status_code == 400
        assert client.post("/login", json={"email": ANA, "code": code}).status_code == 400

    def test_unknown_addresses_get_the_same_answer(self, client: TestClient) -> None:
        known = client.post("/login/code", json={"email": ANA})
        unknown = client.post("/login/code", json={"email": "eve@example.com"})
        assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())
        assert [to for to, _ in main.outbox] == [ANA]

    def test_three_codes_per_address_every_15_minutes(self, client: TestClient) -> None:
        # An address of its own: the limiter remembers blocked clients for the whole process.
        email = f"{secrets.token_hex(8)}@example.com"
        for _ in range(3):
            assert client.post("/login/code", json={"email": email}).status_code == 202
        refused = client.post("/login/code", json={"email": email})
        assert refused.status_code == 429
        assert int(refused.headers["retry-after"]) > 0
        assert client.post("/login/code", json={"email": ANA}).status_code == 202


class TestAccessTokens:
    def test_a_token_is_needed(self, client: TestClient) -> None:
        response = client.post("/api-keys", json={"scopes": ["orders:read"]})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_forged_tokens_are_refused(self, client: TestClient) -> None:
        token = login(client)["Authorization"].removeprefix("Bearer ")
        payload, signature = token.split(".")
        assert not accepted(client, {"Authorization": f"Bearer x{payload}.{signature}"})
        assert not accepted(client, {"Authorization": "Bearer not-a-token"})

    def test_tokens_expire(self, client: TestClient, clock: Clock) -> None:
        auth = login(client)
        clock.advance(TOKEN_LIFETIME - 1)
        assert accepted(client, auth)
        clock.advance(1)
        assert not accepted(client, auth)

    def test_logout_revokes_the_token(self, client: TestClient) -> None:
        auth, other = login(client), login(client)
        assert client.post("/logout", headers=auth).status_code == 204
        assert not accepted(client, auth)
        assert client.post("/logout", headers=auth).status_code == 401
        assert accepted(client, other)

    def test_logout_everywhere(self, client: TestClient, clock: Clock) -> None:
        laptop, phone, bens = login(client), login(client), login(client, BEN)
        clock.advance(1)  # tokens have whole seconds: those of this second would stay valid
        response = client.post("/logout/everywhere", headers=laptop)
        assert response.status_code == 200
        fresh = {"Authorization": f"Bearer {response.json()['access_token']}"}
        assert not accepted(client, laptop)
        assert not accepted(client, phone)
        assert accepted(client, fresh)
        assert accepted(client, bens)


class TestAPIKeys:
    def test_a_key_is_shown_once(self, client: TestClient) -> None:
        response = client.post("/api-keys", json={"scopes": ["orders:read"]}, headers=login(client))
        created = response.json()
        key, key_id = created["key"], created["id"]
        assert key.startswith(f"acme_{key_id}_")
        assert created["scopes"] == ["orders:read"]
        expires_at = datetime.fromisoformat(created["expires_at"])
        assert timedelta(days=89) < expires_at - datetime.now(UTC) <= timedelta(days=90)
        row = main.db.api_keys[key_id]
        assert (row.owner, row.expires_at) == (ANA, expires_at)
        assert key.split("_")[-1] not in repr(row)  # only a digest of the secret

    def test_scopes(self, client: TestClient) -> None:
        auth = login(client)
        reader = new_key(client, auth, "orders:read")
        writer = new_key(client, auth, "orders:read", "orders:write")
        order = {"item": "Olive oil", "quantity": 2}
        refused = client.post("/orders", json=order, headers=reader)
        assert (refused.status_code, refused.json()) == (403, {"detail": "Insufficient scope"})
        assert client.post("/orders", json=order, headers=writer).status_code == 201
        assert client.get("/orders", headers=reader).json() == [order]

    def test_only_known_scopes_are_granted(self, client: TestClient) -> None:
        auth = login(client)
        for scopes in (["admin"], []):
            response = client.post("/api-keys", json={"scopes": scopes}, headers=auth)
            assert response.status_code == 422

    def test_keys_see_their_owners_orders(self, client: TestClient) -> None:
        anas = new_key(client, login(client), "orders:read", "orders:write")
        bens = new_key(client, login(client, BEN), "orders:read")
        client.post("/orders", json={"item": "Figs", "quantity": 1}, headers=anas)
        assert client.get("/orders", headers=bens).json() == []
        assert len(client.get("/orders", headers=anas).json()) == 1

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"X-API-Key": "not-a-key"},
            {"X-API-Key": f"acme_{'a' * 16}_{'b' * 52}"},  # well-formed, unknown
        ],
    )
    def test_bad_keys_get_one_answer(self, client: TestClient, headers: dict[str, str]) -> None:
        response = client.get("/orders", headers=headers)
        assert (response.status_code, response.json()) == (401, {"detail": "Invalid API key"})
        assert response.headers["www-authenticate"] == "APIKey"

    def test_a_wrong_secret_is_refused(self, client: TestClient) -> None:
        key = new_key(client, login(client), "orders:read")["X-API-Key"]
        wrong = key[:-1] + ("a" if key[-1] != "a" else "b")
        assert client.get("/orders", headers={"X-API-Key": wrong}).status_code == 401

    def test_expired_keys_are_refused(self, client: TestClient) -> None:
        key = new_key(client, login(client), "orders:read")
        assert len(main.db.api_keys) == 1
        row = next(iter(main.db.api_keys.values()))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)  # as if 90 days went by
        assert client.get("/orders", headers=key).status_code == 401

    def test_revoked_keys_are_refused_at_once(self, client: TestClient) -> None:
        auth = login(client)
        key = new_key(client, auth, "orders:read")
        assert client.get("/orders", headers=key).status_code == 200  # now in the record cache
        assert len(main.db.api_keys) == 1
        key_id = next(iter(main.db.api_keys))
        assert client.delete(f"/api-keys/{key_id}", headers=login(client, BEN)).status_code == 404
        assert client.delete(f"/api-keys/{key_id}", headers=auth).status_code == 204
        assert client.get("/orders", headers=key).status_code == 401
        assert main.db.api_keys == {}
