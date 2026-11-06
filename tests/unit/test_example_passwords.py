"""The passwords example: sign-up, login and password change, with weak, common and leaked
passwords refused. Have I Been Pwned is a mock: no test calls it."""

import hashlib
import logging
from collections.abc import Iterator

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from examples.passwords import main
from examples.passwords.main import app
from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.auth import PwnedPasswords
from fastapi_alcazaba.auth.passwords import ScryptParameters

STRONG = "sunrise velvet harbour lantern"  # noqa: S105
OTHER = "quiet orchard copper meadow"  # noqa: S105
LEAKED = "correct horse battery staple"  # noqa: S105  # in the mock's leaks
ANA = {"email": "ana@example.com", "name": "Ana Belén Zurbarán"}


def leaks(*leaked: str) -> httpx.AsyncClient:
    """Stands in for the Pwned Passwords range API."""

    def answer(request: httpx.Request) -> httpx.Response:
        prefix = request.url.path.rsplit("/", 1)[-1]
        lines = ["0" * 35 + ":0"]
        for password in leaked:
            sha1 = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()
            if sha1.startswith(prefix):
                lines.append(f"{sha1[5:]}:1234")
        return httpx.Response(200, text="\n".join(lines))

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


def down() -> httpx.AsyncClient:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(fail))


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(main, "users", {})
    monkeypatch.setattr(main.passwords, "pwned", PwnedPasswords(client=leaks(LEAKED)))
    monkeypatch.setattr(main.passwords, "parameters", ScryptParameters(log_n=14))  # faster
    with TestClient(app) as client:
        yield client
    FastAPICache.reset()


def signup(client: TestClient, password: str) -> httpx.Response:
    return client.post("/signup", json={**ANA, "password": password})


def login(client: TestClient, password: str, email: str = ANA["email"]) -> int:
    return client.post("/login", json={"email": email, "password": password}).status_code


def test_a_strong_password(client: TestClient) -> None:
    response = signup(client, STRONG)
    assert response.status_code == 201
    assert response.json() == {"id": 1, **ANA}
    assert main.users["ana@example.com"].password.startswith("$scrypt$")
    assert login(client, STRONG, email="ANA@example.com") == 200


@pytest.mark.parametrize(
    ("password", "problems"),
    [
        ("short", ["too_short", "weak"]),
        ("ana@example.com is my password", ["contains_user_input"]),
        ("zurbaranbelen1984", ["contains_user_input"]),  # her name
        ("zurb4r4nzurb4r4n1984", ["weak"]),  # her name in l33t: zxcvbn knows it
        ("qwertyuiopasdfghjkl", ["weak"]),  # long, but a keyboard walk
        ("LIGHTHOUSE NOTES", ["blocklisted"]),  # the app's own name
        (LEAKED, ["pwned"]),
    ],
)
def test_weak_common_and_leaked_passwords(
    client: TestClient,
    password: str,
    problems: list[str],
) -> None:
    response = signup(client, password)
    assert response.status_code == 422
    assert response.json()["detail"]["password"] == problems
    assert main.users == {}


def test_advice_comes_with_a_weak_one(client: TestClient) -> None:
    detail = signup(client, "qwertyuiopasdfghjkl").json()["detail"]
    assert "Add another word or two. Uncommon words are better." in detail["suggestions"]


def test_a_password_too_long_or_not_text(client: TestClient) -> None:
    assert signup(client, "x" * 2000).status_code == 422  # the model's max_length
    body = b'{"email": "ana@example.com", "name": "Ana", "password": "\\ud800 lone surrogate pw"}'
    response = client.post("/signup", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["detail"]["password"] == ["invalid_characters"]


def test_hibp_down_fails_open(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(main.passwords, "pwned", PwnedPasswords(client=down()))
    caplog.set_level(logging.DEBUG)
    assert signup(client, STRONG).status_code == 201
    assert "Couldn't check a password against the leaks: ConnectTimeout" in caplog.text
    assert STRONG not in caplog.text


def test_hibp_down_fails_closed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.passwords, "pwned", PwnedPasswords(client=down(), fail_open=False))
    response = signup(client, STRONG)
    assert response.status_code == 503
    assert response.json() == {"detail": "Try again in a moment"}
    assert main.users == {}


def test_changing_the_password(client: TestClient) -> None:
    signup(client, STRONG)
    change = {"email": ANA["email"], "current_password": STRONG, "new_password": OTHER}
    assert client.post("/password/change", json=change).status_code == 204
    assert login(client, STRONG) == 401
    assert login(client, OTHER) == 200


@pytest.mark.parametrize(
    ("current", "new", "status", "problems"),
    [
        ("wrong current password", OTHER, 401, None),
        (STRONG, STRONG, 422, ["unchanged"]),
        (STRONG, "Ana Belén Zurbarán!", 422, ["contains_user_input", "weak"]),
        (STRONG, LEAKED, 422, ["pwned"]),
    ],
)
def test_a_change_refused(
    client: TestClient,
    current: str,
    new: str,
    status: int,
    problems: list[str] | None,
) -> None:
    signup(client, STRONG)
    change = {"email": ANA["email"], "current_password": current, "new_password": new}
    response = client.post("/password/change", json=change)
    assert response.status_code == status
    if problems is not None:
        assert response.json()["detail"]["password"] == problems
    assert login(client, STRONG) == 200


def test_an_unknown_account_and_a_wrong_password_look_alike(client: TestClient) -> None:
    signup(client, STRONG)
    wrong = client.post("/login", json={"email": ANA["email"], "password": OTHER})
    unknown = client.post("/login", json={"email": "ben@example.com", "password": OTHER})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
