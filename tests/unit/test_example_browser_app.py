"""The browser app example: server-side sessions, CSRF protection and security headers."""

import re
import secrets
import time
from collections.abc import Iterator

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from examples.browser_app.main import CSP, app
from fastapi_alcazaba import FastAPICache

PASSWORD = "correct horse battery staple"  # noqa: S105  # the example's demo user
# What a browser sends with a fetch from the app's own page.
SAME_ORIGIN = {
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Origin": "https://testserver",
}
# A form on another site, posted to the app.
CROSS_SITE = {
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "navigate",
    "Origin": "https://evil.example",
}
SESSION_COOKIE = re.compile(
    r"__Host-session=([\w-]{43}); Max-Age=28800; Path=/; HttpOnly; Secure; SameSite=Lax"
)


def browser(address: str = "testclient") -> TestClient:
    # https: the cookies are Secure, so the client only sends them back over https.
    return TestClient(app, base_url="https://testserver", client=(address, 50000))


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    with browser() as client:
        client.get("/")  # the first page sets the CSRF token, as in a browser
        yield client
    FastAPICache.reset()


class Clock:
    """Moves time.time forward, which the sessions read."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.offset = 0.0
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + self.offset)

    def advance(self, seconds: float) -> None:
        self.offset += seconds


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


def send(client: TestClient, path: str, **data: str) -> httpx.Response:
    """What app.js does: post a form from the app's page with the CSRF token in the header."""
    headers = {**SAME_ORIGIN, "X-CSRF-Token": client.cookies["__Host-csrftoken"]}
    return client.post(path, data=data, headers=headers, follow_redirects=False)


def sign_in(client: TestClient, username: str = "ana", password: str = PASSWORD) -> httpx.Response:
    return send(client, "/login", username=username, password=password)


def session_id(response: httpx.Response) -> str:
    match = SESSION_COOKIE.fullmatch(response.headers["set-cookie"])
    assert match is not None, response.headers.get("set-cookie")
    return match.group(1)


def home_with(session: str) -> str:
    """The home page of another browser that copied the session cookie `session`."""
    with browser() as other:
        return other.get("/", headers={"Cookie": f"__Host-session={session}"}).text


def signed_in_as(page: str) -> str | None:
    match = re.search(r"Signed in as (\w+)\.", page)
    return match.group(1) if match else None


class TestPages:
    def test_visitors_get_no_session_until_they_need_one(self, client: TestClient) -> None:
        page = client.get("/")
        assert '<a href="/login">Sign in</a>' in page.text
        assert "__Host-session" not in client.cookies

    def test_the_page_carries_the_csrf_token(self) -> None:
        with browser() as client:
            page = client.get("/")
        token = client.cookies["__Host-csrftoken"]
        assert (
            page.headers["set-cookie"] == f"__Host-csrftoken={token}; Path=/; Secure; SameSite=Lax"
        )
        assert f'<meta name="csrf-token" content="{token}">' in page.text
        assert client.get("/").headers.get("set-cookie") is None  # kept while it's valid

    def test_the_script_sends_it_in_a_header(self, client: TestClient) -> None:
        script = client.get("/app.js")
        assert script.headers["content-type"].startswith("text/javascript")
        assert '"X-CSRF-Token": token' in script.text

    def test_the_login_page(self, client: TestClient) -> None:
        assert "Wrong username or password" not in client.get("/login").text
        assert "Wrong username or password" in client.get("/login?failed=true").text


class TestCart:
    def test_the_cart_is_kept_in_the_session(self, client: TestClient) -> None:
        added = send(client, "/cart", product="Figs")
        assert (added.status_code, added.headers["location"]) == (303, "/")
        session_id(added)
        send(client, "/cart", product="Figs")
        send(client, "/cart", product="Almonds")
        assert "<li>Almonds: 1</li><li>Figs: 2</li>" in client.get("/").text  # in shop order

    def test_it_holds_what_the_shop_sells_and_little_of_it(self, client: TestClient) -> None:
        assert send(client, "/cart", product="Gold").status_code == 422
        for _ in range(11):
            send(client, "/cart", product="Figs")
        assert "<li>Figs: 10</li>" in client.get("/").text


class TestSigningIn:
    def test_wrong_passwords(self, client: TestClient) -> None:
        for username, password in [("ana", "wrong"), ("nobody", PASSWORD)]:
            response = sign_in(client, username, password)
            assert (response.status_code, response.headers["location"]) == (
                303,
                "/login?failed=true",
            )
            assert "set-cookie" not in response.headers
        assert signed_in_as(client.get("/").text) is None

    def test_signing_in_gives_a_new_session_id(self, client: TestClient) -> None:
        before = session_id(send(client, "/cart", product="Figs"))
        signed_in = sign_in(client)
        assert (signed_in.status_code, signed_in.headers["location"]) == (303, "/")
        assert signed_in.headers["cache-control"] == "no-store"
        after = session_id(signed_in)
        assert after != before
        page = client.get("/").text
        assert (signed_in_as(page), "<li>Figs: 1</li>" in page) == ("ana", True)  # cart kept
        old = home_with(before)  # an id planted before the login is worth nothing now
        assert (signed_in_as(old), "Figs" in old.split("<h2>Cart</h2>")[1]) == (None, False)
        assert signed_in_as(home_with(after)) == "ana"

    def test_signing_out_deletes_the_session(self, client: TestClient) -> None:
        session = session_id(sign_in(client))
        signed_out = send(client, "/logout")
        assert signed_out.headers["set-cookie"].startswith("__Host-session=; Max-Age=0;")
        assert signed_in_as(client.get("/").text) is None
        assert signed_in_as(home_with(session)) is None  # a copied cookie doesn't work either

    def test_five_attempts_a_minute(self, client: TestClient) -> None:
        # An address of its own: the limiter remembers blocked clients for the whole process.
        with browser("203.0.113.7") as other:
            other.get("/")
            for _ in range(5):
                assert sign_in(other, password="guess").status_code == 303  # noqa: S106
            refused = sign_in(other)
        assert refused.status_code == 429
        assert int(refused.headers["retry-after"]) > 0
        assert sign_in(client).status_code == 303


class TestIdleTimeout:
    def test_requests_keep_the_session_alive(self, client: TestClient, clock: Clock) -> None:
        sign_in(client)
        for _ in range(3):
            clock.advance(29 * 60)
            assert signed_in_as(client.get("/").text) == "ana"

    def test_30_minutes_without_requests_end_it(self, client: TestClient, clock: Clock) -> None:
        sign_in(client)
        clock.advance(30 * 60)
        assert signed_in_as(client.get("/").text) is None


class TestCSRF:
    def test_the_token_is_required(self, client: TestClient) -> None:
        for token in (None, secrets.token_urlsafe(32)):
            headers = SAME_ORIGIN if token is None else {**SAME_ORIGIN, "X-CSRF-Token": token}
            response = client.post("/cart", data={"product": "Figs"}, headers=headers)
            assert (response.status_code, response.json()) == (
                403,
                {"detail": "Missing or invalid CSRF token"},
            )
        assert "Figs:" not in client.get("/").text

    def test_other_sites_are_refused(self, client: TestClient) -> None:
        sign_in(client)
        # Even with the token, which a page on another site couldn't read anyway.
        headers = {**CROSS_SITE, "X-CSRF-Token": client.cookies["__Host-csrftoken"]}
        response = client.post("/logout", headers=headers, follow_redirects=False)
        assert (response.status_code, response.json()) == (
            403,
            {"detail": "Cross-site request refused"},
        )
        assert signed_in_as(client.get("/").text) == "ana"

    def test_another_site_cant_sign_the_browser_in(self) -> None:
        # Login CSRF: before the first page there are no cookies, and still the form is refused.
        form = {"username": "ana", "password": PASSWORD}
        with browser() as fresh:
            response = fresh.post("/login", data=form, headers=CROSS_SITE)
        assert response.status_code == 403

    def test_links_from_other_sites_work(self, client: TestClient) -> None:
        assert client.get("/", headers=CROSS_SITE).status_code == 200


class TestSecurityHeaders:
    def test_pages(self, client: TestClient) -> None:
        headers = client.get("/").headers
        assert headers["content-security-policy"] == CSP
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["cross-origin-opener-policy"] == "same-origin"
        assert headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"

    def test_refusals_get_them_too(self, client: TestClient) -> None:
        refused = client.post("/cart", data={"product": "Figs"}, headers=CROSS_SITE)
        assert refused.status_code == 403
        assert (
            refused.headers["content-security-policy"]
            == "default-src 'none'; frame-ancestors 'none'"
        )
        assert refused.headers["x-frame-options"] == "DENY"

    def test_hsts_only_over_https(self) -> None:
        with TestClient(app) as plain:
            assert "strict-transport-security" not in plain.get("/").headers

    def test_large_bodies_are_refused(self, client: TestClient) -> None:
        response = send(client, "/cart", product="Figs", padding="x" * 65_536)
        assert (response.status_code, response.json()) == (
            413,
            {"detail": "Request body too large"},
        )
        assert response.headers["x-content-type-options"] == "nosniff"
