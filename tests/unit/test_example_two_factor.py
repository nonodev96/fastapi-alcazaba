"""The second factor example: the password, then an authenticator app's code or a recovery code,
and a recent one to delete the account."""

# pylint: disable=protected-access  # the codes an app would show, from RFC 4226's function

import base64
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from examples.two_factor import main
from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.auth import stepup
from fastapi_alcazaba.auth import totp as totp_module

SAME_SITE = {"Sec-Fetch-Site": "same-origin"}  # what browsers send with the app's own requests
LOGIN = {"username": "ana", "password": main.DEMO_PASSWORD}


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(totp_module, "time", clock)
    monkeypatch.setattr(stepup, "time", clock)
    return clock


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Iterator[TestClient]:
    del clock  # frozen before the app starts
    monkeypatch.setattr(main, "db", main.Database())
    with new_browser() as client:
        yield client
    FastAPICache.reset()


def new_browser() -> TestClient:
    return TestClient(main.app, base_url="https://testserver", headers=SAME_SITE)


def app_code(secret: str, clock: Clock, steps: int = 0) -> str:
    """What the authenticator app shows: RFC 6238's code of the secret it scanned."""
    counter = int(clock.now // 30) + steps
    return totp_module._counter_code(base64.b32decode(secret), counter, 6, "SHA1")


def stored_secret() -> str | None:
    """What the database keeps of Ana's app."""
    return main.db.users["u1"].totp


def enroll(client: TestClient, clock: Clock) -> tuple[str, list[str]]:
    """Ana, logged in with her password, adds her app: its secret and her recovery codes."""
    assert client.post("/login", json=LOGIN).json() == {"logged_in": "ana"}
    enrollment = client.post("/totp/enroll").json()
    assert enrollment["uri"].startswith("otpauth://totp/Example:ana?secret=")
    secret: str = enrollment["secret"]
    confirmed = client.post("/totp/confirm", json={"code": app_code(secret, clock)})
    assert confirmed.status_code == 200
    codes: list[str] = confirmed.json()["recovery_codes"]
    return secret, codes


def test_enrolling(client: TestClient, clock: Clock) -> None:
    assert client.post("/totp/confirm", json={"code": "123456"}).status_code == 401  # nobody
    assert client.post("/login", json=LOGIN).status_code == 200
    assert client.post("/totp/confirm", json={"code": "123456"}).status_code == 409
    secret = client.post("/totp/enroll").json()["secret"]
    wrong = f"{(int(app_code(secret, clock)) + 1) % 10**6:06d}"
    refused = client.post("/totp/confirm", json={"code": wrong})
    assert refused.status_code == 400
    assert refused.json() == {"detail": "Wrong code: try the next one"}
    assert stored_secret() is None  # not on until a code of it is right
    secret, codes = enroll(new_browser(), clock)
    assert len(codes) == 10
    sealed = stored_secret()
    assert sealed is not None
    assert secret not in sealed  # stored sealed


def test_logins_need_the_second_factor(client: TestClient, clock: Clock) -> None:
    secret, _ = enroll(client, clock)
    browser = new_browser()
    assert browser.post("/login", json=LOGIN).json() == {"next": "/login/totp"}
    assert browser.get("/me").status_code == 401  # halfway in is not in
    clock.now += 30
    assert browser.post("/login/totp", json={"code": app_code(secret, clock)}).status_code == 200
    assert browser.get("/me").json() == {"username": "ana", "second_factor": True}


def test_a_code_works_once(client: TestClient, clock: Clock) -> None:
    secret, _ = enroll(client, clock)
    clock.now += 30
    code = app_code(secret, clock)
    first, second = new_browser(), new_browser()
    for browser in (first, second):
        browser.post("/login", json=LOGIN)
    assert first.post("/login/totp", json={"code": code}).status_code == 200
    assert second.post("/login/totp", json={"code": code}).json() == {"detail": "Wrong code"}


def test_the_totp_step_needs_the_password_first(client: TestClient) -> None:
    refused = client.post("/login/totp", json={"code": "123456"})
    assert refused.json() == {"detail": "Log in with your password first"}
    assert client.post("/login/recovery", json={"code": "AAAAA-AAAAA"}).status_code == 401


def test_wrong_passwords(client: TestClient) -> None:
    for login in ({**LOGIN, "password": "guess"}, {"username": "nobody", "password": "x"}):
        refused = client.post("/login", json=login)
        assert refused.json() == {"detail": "Wrong username or password"}


def test_recovery_codes(client: TestClient, clock: Clock) -> None:
    _, codes = enroll(client, clock)
    browser = new_browser()
    browser.post("/login", json=LOGIN)
    used = browser.post("/login/recovery", json={"code": codes[0]})
    assert used.json() == {"logged_in": "ana", "recovery_codes_left": 9}
    again = new_browser()
    again.post("/login", json=LOGIN)
    assert again.post("/login/recovery", json={"code": codes[0]}).status_code == 401


def test_deleting_the_account_needs_a_recent_code(client: TestClient, clock: Clock) -> None:
    assert client.delete("/account").status_code == 401
    client.post("/login", json=LOGIN)
    refused = client.delete("/account")
    assert (refused.status_code, refused.json()) == (403, {"detail": "Second factor required"})
    secret, _ = enroll(new_browser(), clock)
    browser = new_browser()
    browser.post("/login", json=LOGIN)
    clock.now += 30
    browser.post("/login/totp", json={"code": app_code(secret, clock)})
    clock.now += 301
    assert browser.delete("/account").status_code == 403  # too long ago
    clock.now += 30
    browser.post("/login", json=LOGIN)
    browser.post("/login/totp", json={"code": app_code(secret, clock)})
    assert browser.delete("/account").status_code == 204
    assert "u1" not in main.db.users
