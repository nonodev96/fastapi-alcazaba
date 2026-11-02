"""The passkeys example: a site without passwords, its sign-ups and logins by passkey, and the
devices each user is logged in on."""

import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.passkeys import main
from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.auth import stepup
from tests.unit.test_webauthn import Authenticator

SAME_SITE = {"Sec-Fetch-Site": "same-origin"}  # what browsers send with the site's own requests


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(main, "db", main.Database())
    with browser() as client:
        yield client
    FastAPICache.reset()


def browser(agent: str = "laptop") -> TestClient:
    client = TestClient(main.app, base_url="https://testserver", headers=SAME_SITE)
    client.headers["User-Agent"] = agent
    return client


def authenticator() -> Authenticator:
    """The user's passkey, for the example's site."""
    found = Authenticator(rp_id="localhost")
    found.origin = "http://localhost:8000"
    return found


def sign_up(client: TestClient, passkey: Authenticator, username: str = "ana") -> Any:
    options = client.post("/signup/options", json={"username": username}).json()
    assert options["rp"] == {"id": "localhost", "name": "Example"}
    return client.post("/signup", json=passkey.create(options))


def log_in(client: TestClient, passkey: Authenticator, handle: str) -> Any:
    options = client.post("/login/options").json()
    return client.post("/login", json=passkey.get(options, handle))


def handle_of(username: str) -> str:
    user = main.db.by_name(username)
    assert user is not None
    return user.handle


def test_signing_up_logs_in(client: TestClient) -> None:
    assert client.get("/me").status_code == 401
    assert sign_up(client, authenticator()).json() == {"logged_in": "ana"}
    assert client.get("/me").json() == {"username": "ana", "passkeys": 1}
    taken = browser().post("/signup/options", json={"username": "ana"})
    assert (taken.status_code, taken.json()) == (409, {"detail": "That username is taken"})


def test_logging_in_without_typing_anything(client: TestClient) -> None:
    passkey = authenticator()
    sign_up(client, passkey)
    phone = browser("phone")
    assert log_in(phone, passkey, handle_of("ana")).json() == {"logged_in": "ana"}
    assert phone.get("/me").json()["username"] == "ana"


def test_a_passkey_the_site_doesnt_know(client: TestClient) -> None:
    sign_up(client, authenticator())
    stranger = authenticator()
    stranger.credential_id = b"\x02" * 18
    refused = log_in(browser(), stranger, handle_of("ana"))
    assert refused.json() == {"detail": "That passkey wasn't accepted"}


def test_a_passkey_of_another_site(client: TestClient) -> None:
    passkey = authenticator()
    passkey.origin = "https://evil.example"
    assert sign_up(client, passkey).status_code == 400


def test_signing_up_needs_the_options_first(client: TestClient) -> None:
    refused = client.post("/signup", json={"type": "public-key"})
    assert refused.json() == {"detail": "Ask for the options first"}


def test_the_devices(client: TestClient) -> None:
    passkey = authenticator()
    sign_up(client, passkey)
    phone, tablet = browser("phone"), browser("tablet")
    for device in (phone, tablet):
        log_in(device, passkey, handle_of("ana"))
    devices = client.get("/sessions").json()
    assert sorted(d["device"] for d in devices) == ["laptop", "phone", "tablet"]
    assert [d["device"] for d in devices if d["this_one"]] == ["laptop"]
    ref = next(d["ref"] for d in devices if d["device"] == "phone")
    assert client.delete(f"/sessions/{ref}").status_code == 204
    assert phone.get("/me").status_code == 401
    assert client.delete(f"/sessions/{ref}").status_code == 404
    assert client.post("/sessions/end-all").json() == {"ended": 1}
    assert tablet.get("/me").status_code == 401
    assert client.get("/me").status_code == 200


def test_deleting_the_account_needs_a_recent_passkey(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passkey = authenticator()
    sign_up(client, passkey)
    later = SimpleNamespace(time=lambda: time.time() + 301)  # step-up's clock, 5 minutes on
    monkeypatch.setattr(stepup, "time", later)
    refused = client.delete("/account")
    assert (refused.status_code, refused.json()) == (403, {"detail": "Second factor required"})
    monkeypatch.setattr(stepup, "time", time)  # back to now
    assert client.delete("/account").status_code == 204
    assert main.db.users == {}
    assert client.get("/me").status_code == 401


def test_logging_out(client: TestClient) -> None:
    sign_up(client, authenticator())
    assert client.post("/logout").status_code == 204
    assert client.get("/me").status_code == 401


def test_the_page(client: TestClient) -> None:
    page = client.get("/").text
    assert "navigator.credentials.create" in page
    assert "parseRequestOptionsFromJSON" in page
