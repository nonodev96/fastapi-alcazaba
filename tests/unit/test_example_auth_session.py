"""The session example: a password starts a server-side session, which logging out deletes."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from examples.auth_strategies import session_login
from fastapi_alcazaba import FastAPICache

SAME_SITE = {"Sec-Fetch-Site": "same-origin"}  # what browsers send with the app's own requests
OCTOCAT = {"id": "1", "login": "octocat", "name": "The Octocat"}
DEMO = {"login": "octocat", "password": session_login.DEMO_PASSWORD}


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(session_login, "db", session_login.Database())
    # https: the session cookie is Secure.
    with TestClient(session_login.app, base_url="https://testserver") as client:
        yield client
    FastAPICache.reset()


class TestSessionExample:
    def test_nobody_without_a_session(self, client: TestClient) -> None:
        assert client.get("/me").status_code == 401

    def test_logging_in_starts_a_session(self, client: TestClient) -> None:
        logged_in = client.post("/login", json=DEMO, headers=SAME_SITE)
        assert (logged_in.status_code, logged_in.json()) == (200, OCTOCAT)
        cookie = logged_in.headers["set-cookie"]
        assert cookie.startswith("session=")
        assert all(attribute in cookie for attribute in ("HttpOnly", "Secure", "SameSite=Lax"))
        assert client.get("/me").json() == OCTOCAT

    def test_a_wrong_password(self, client: TestClient) -> None:
        wrong = DEMO | {"password": "guess"}
        assert client.post("/login", json=wrong, headers=SAME_SITE).status_code == 401
        assert "set-cookie" not in client.post("/login", json=wrong, headers=SAME_SITE).headers

    def test_a_new_session_id_at_login(self, client: TestClient) -> None:
        client.post("/login", json=DEMO, headers=SAME_SITE)
        before = client.cookies["session"]
        client.post("/login", json=DEMO, headers=SAME_SITE)  # again, with that session
        assert client.cookies["session"] != before
        client.cookies.set("session", before, domain="testserver.local")
        assert client.get("/me").status_code == 401  # the old id is gone

    def test_logging_out(self, client: TestClient) -> None:
        client.post("/login", json=DEMO, headers=SAME_SITE)
        copy = client.cookies["session"]
        assert client.post("/logout", headers=SAME_SITE).status_code == 204
        assert client.get("/me").status_code == 401
        # The session is gone from the backend: a copy of the cookie is refused too.
        client.cookies.set("session", copy, domain="testserver.local")
        assert client.get("/me").status_code == 401

    def test_other_sites_cant_log_you_in_or_out(self, client: TestClient) -> None:
        cross_site = {"Sec-Fetch-Site": "cross-site"}
        assert client.post("/login", json=DEMO, headers=cross_site).status_code == 403
        client.post("/login", json=DEMO, headers=SAME_SITE)
        assert client.post("/logout", headers=cross_site).status_code == 403
        assert client.get("/me").status_code == 200
