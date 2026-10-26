"""CSRFMiddleware: requests that another site made the user's browser send are refused."""

import logging
from typing import Any

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.types import Message, Scope
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba.csrf import CSRFMiddleware, _origin

COOKIE = {"Cookie": "session=abc"}
OWN = "https://testserver"
EVIL = "https://evil.example"
REFUSED = {"detail": "Cross-site request refused"}


def make_app(**options: Any) -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware, **options)

    @app.get("/items")
    async def listing(request: Request) -> dict[str, Any]:
        return {"token": getattr(request.state, "csrf_token", None)}

    @app.post("/items")
    async def create() -> dict[str, bool]:
        return {"created": True}

    @app.options("/items")
    async def preflight() -> dict[str, bool]:
        return {"options": True}

    @app.post("/webhooks/github")
    async def github() -> dict[str, bool]:
        return {"received": True}

    @app.post("/webhooksx")
    async def not_a_webhook() -> dict[str, bool]:
        return {"received": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    @app.websocket("/public/ws")
    async def public_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("public")
        await websocket.close()

    return app


def client(**options: Any) -> TestClient:
    return TestClient(make_app(**options), base_url=OWN)


def post(test_client: TestClient, path: str = "/items", **headers: str) -> Any:
    return test_client.post(path, headers=headers)


class TestSafeMethods:
    def test_safe_methods_always_pass(self) -> None:
        app = client()
        headers = {**COOKIE, "Sec-Fetch-Site": "cross-site", "Origin": EVIL}
        assert app.get("/items", headers=headers).status_code == 200
        assert app.options("/items", headers=headers).status_code == 200


class TestWithoutCredentials:
    def test_requests_without_cookies_pass(self) -> None:
        assert post(client()).json() == {"created": True}

    def test_cross_origin_api_calls_pass(self) -> None:
        response = post(client(), **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"})
        assert response.status_code == 200

    @pytest.mark.parametrize("mode", ["navigate", "no-cors", "nested-navigate"])
    def test_form_submissions_from_other_sites_are_refused(self, mode: str) -> None:
        """Login CSRF: the browser has no cookies yet, the attacker's form logs it in."""
        response = post(client(), **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": mode})
        assert (response.status_code, response.json()) == (403, REFUSED)

    def test_bearer_tokens_are_not_the_browsers(self) -> None:
        assert post(client(), Authorization="Bearer abc").status_code == 200

    @pytest.mark.parametrize("scheme", ["Basic", "digest", "Negotiate", "NTLM"])
    def test_http_authentication_is_checked_like_cookies(self, scheme: str) -> None:
        response = post(client(), Authorization=f"{scheme} abc", Origin=EVIL)
        assert response.status_code == 403

    def test_an_empty_authorization_header(self) -> None:
        assert post(client(), Authorization="").status_code == 200

    def test_an_empty_cookie_header_isnt_a_cookie(self) -> None:
        assert post(client(), Cookie=" ").status_code == 200

    def test_check_all(self) -> None:
        app = client(check_all=True)
        assert post(app).status_code == 403
        assert post(app, Origin=OWN).status_code == 200
        cors = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"}
        assert post(app, **cors).status_code == 403


class TestFetchMetadata:
    @pytest.mark.parametrize("site", ["same-origin", "none"])
    def test_same_origin_requests_pass(self, site: str) -> None:
        assert post(client(), **COOKIE, **{"Sec-Fetch-Site": site}).status_code == 200

    @pytest.mark.parametrize("site", ["cross-site", "same-site", "something-new"])
    def test_other_sites_are_refused(self, site: str) -> None:
        response = post(client(), **COOKIE, **{"Sec-Fetch-Site": site, "Origin": OWN})
        assert (response.status_code, response.json()) == (403, REFUSED)

    def test_trusted_origins(self) -> None:
        app = client(trusted_origins=["https://admin.example.com/"])
        headers = {**COOKIE, "Sec-Fetch-Site": "same-site"}
        assert post(app, **headers, Origin="https://ADMIN.example.com:443").status_code == 200
        assert post(app, **headers, Origin="https://other.example.com").status_code == 403
        assert post(app, **headers).status_code == 403

    def test_refusals_are_logged_without_secrets(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, "fastapi_alcazaba.csrf"):
            post(client(), Cookie="session=s3cr3t", **{"Sec-Fetch-Site": "cross-site"})
        assert "Refused a cross-site POST request: Sec-Fetch-Site 'cross-site'" in caplog.text
        assert "s3cr3t" not in caplog.text


class TestOrigin:
    @pytest.mark.parametrize("origin", [OWN, "https://testserver:443", "HTTPS://TestServer"])
    def test_the_apps_own_origin(self, origin: str) -> None:
        assert post(client(), **COOKIE, Origin=origin).status_code == 200

    @pytest.mark.parametrize(
        "origin",
        [EVIL, "http://testserver", "https://testserver:8443", "null", "https://testserver/x"],
    )
    def test_other_origins_are_refused(self, origin: str) -> None:
        response = post(client(), **COOKIE, Origin=origin)
        assert (response.status_code, response.json()) == (403, REFUSED)

    def test_a_null_origin_doesnt_fall_back_to_the_referer(self) -> None:
        response = post(client(), **COOKIE, Origin="null", Referer=f"{OWN}/form")
        assert response.status_code == 403

    def test_the_referer_when_theres_no_origin(self) -> None:
        app = client(trusted_origins=[EVIL])
        assert post(app, **COOKIE, Referer=f"{OWN}/form?x=1").status_code == 200
        assert post(app, **COOKIE, Referer=f"{EVIL}/page").status_code == 200
        assert post(app, **COOKIE, Referer="https://other.example/page").status_code == 403
        assert post(app, **COOKIE, Referer="not a url").status_code == 403

    def test_neither_origin_nor_referer(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, "fastapi_alcazaba.csrf"):
            response = post(client(), **COOKIE)
        assert (response.status_code, response.json()) == (403, REFUSED)
        assert "neither Origin nor Referer" in caplog.text

    async def test_repeated_origins_match_nothing(self) -> None:
        own = OWN.encode()
        headers = [
            (b"host", b"testserver"),
            (b"cookie", b"a=1"),
            (b"origin", own),
            (b"origin", own),
        ]
        assert await run(make_app(), headers) == 403
        assert await run(make_app(), headers[:-1]) == 200

    async def test_without_a_host_only_trusted_origins_pass(self) -> None:
        app = make_app(trusted_origins=[EVIL])
        assert await run(app, [(b"cookie", b"a=1"), (b"origin", OWN.encode())]) == 403
        assert await run(app, [(b"cookie", b"a=1"), (b"origin", EVIL.encode())]) == 200

    def test_ipv6_hosts_and_other_ports(self) -> None:
        app = TestClient(make_app(), base_url="http://[::1]:8000")
        assert post(app, **COOKIE, Origin="http://[::1]:8000").status_code == 200
        assert post(app, **COOKIE, Origin="http://[::1]").status_code == 403


class TestExemptPaths:
    def test_exempt_paths_arent_checked(self) -> None:
        app = client(exempt_paths=["/webhooks/"])
        headers = {**COOKIE, "Sec-Fetch-Site": "cross-site"}
        assert post(app, "/webhooks/github", **headers).status_code == 200
        assert post(app, "/webhooksx", **headers).status_code == 403
        assert post(app, "/items", **headers).status_code == 403

    def test_exempt_paths_are_matched_without_the_root_path(self) -> None:
        app = TestClient(make_app(exempt_paths=["/webhooks"]), base_url=OWN, root_path="/api")
        headers = {**COOKIE, "Sec-Fetch-Site": "cross-site"}
        assert post(app, "/webhooks/github", **headers).status_code == 200


class TestToken:
    def test_a_token_cookie_is_set_and_exposed(self) -> None:
        app = client(require_token=True)
        response = app.get("/items")
        token = response.json()["token"]
        assert len(token) == 43
        assert response.headers["set-cookie"] == (
            f"__Host-csrftoken={token}; Path=/; Secure; SameSite=Lax"
        )
        again = app.get("/items")  # the client sends it back now
        assert again.json()["token"] == token
        assert "set-cookie" not in again.headers

    def test_requests_with_cookies_need_the_token(self) -> None:
        app = client(require_token=True)
        token = app.get("/items").json()["token"]
        refused = app.post("/items", headers={"Origin": OWN})
        assert (refused.status_code, refused.json()) == (
            403,
            {"detail": "Missing or invalid CSRF token"},
        )
        wrong = app.post("/items", headers={"Origin": OWN, "X-CSRF-Token": token[:-1] + "!"})
        assert wrong.status_code == 403
        accepted = app.post("/items", headers={"Origin": OWN, "X-CSRF-Token": token})
        assert accepted.status_code == 200

    def test_a_valid_token_makes_up_for_a_missing_origin(self) -> None:
        app = client(require_token=True)
        token = app.get("/items").json()["token"]
        assert app.post("/items", headers={"X-CSRF-Token": token}).status_code == 200
        refused = app.post("/items", headers={"X-CSRF-Token": "x" * 43})
        assert (refused.status_code, refused.json()) == (403, REFUSED)

    def test_a_valid_token_doesnt_make_up_for_another_site(self) -> None:
        app = client(require_token=True)
        token = app.get("/items").json()["token"]
        headers = {"X-CSRF-Token": token, "Sec-Fetch-Site": "cross-site", "Origin": EVIL}
        assert app.post("/items", headers=headers).status_code == 403

    def test_the_403_brings_a_token(self) -> None:
        app = client(require_token=True)
        refused = app.post("/items", headers={**COOKIE, "Origin": OWN})
        assert refused.status_code == 403
        assert refused.headers["set-cookie"].startswith("__Host-csrftoken=")

    def test_requests_without_cookies_need_no_token(self) -> None:
        assert post(client(require_token=True)).status_code == 200

    @pytest.mark.parametrize("cookie", ["__Host-csrftoken=short", "__Host-csrftoken=" + "a" * 44])
    def test_malformed_token_cookies_are_replaced(self, cookie: str) -> None:
        response = client(require_token=True).get("/items", headers={"Cookie": cookie})
        assert response.headers["set-cookie"].startswith("__Host-csrftoken=")
        assert response.json()["token"] != cookie.split("=")[1]

    def test_repeated_token_cookies_are_refused(self) -> None:
        """A sibling subdomain added one (cookie tossing): neither is trusted."""
        token = "t" * 43
        cookie = f"__Host-csrftoken={token}; __Host-csrftoken={token}"
        headers = {"Cookie": cookie, "Origin": OWN, "X-CSRF-Token": token}
        assert client(require_token=True).post("/items", headers=headers).status_code == 403

    def test_custom_names_and_domain(self) -> None:
        app = client(
            require_token=True,
            cookie_name="csrf",
            header_name="X-XSRF-TOKEN",
            cookie_secure=False,
            cookie_domain=".example.com",
        )
        response = app.get("/items")
        token = response.json()["token"]
        assert response.headers["set-cookie"] == (
            f"csrf={token}; Path=/; Domain=.example.com; SameSite=Lax"
        )
        headers = {"Cookie": f"csrf={token}", "Origin": OWN, "X-XSRF-Token": token}
        assert app.post("/items", headers=headers).status_code == 200


class TestWebSockets:
    def test_handshakes_without_credentials_pass(self) -> None:
        with client().websocket_connect("/ws", headers={"Origin": EVIL}) as ws:
            assert ws.receive_text() == "hello"

    def test_cross_site_handshakes_are_refused(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            caplog.at_level(logging.INFO, "fastapi_alcazaba.csrf"),
            pytest.raises(WebSocketDisconnect) as refused,
            client().websocket_connect("/ws", headers={**COOKIE, "Origin": EVIL}),
        ):
            pass  # pragma: no cover
        assert refused.value.code == 1008
        assert "Refused a cross-site WebSocket handshake" in caplog.text

    @pytest.mark.parametrize(
        ("url", "origin"),
        [("/ws", "http://testserver"), ("wss://testserver/ws", OWN)],
    )
    def test_the_apps_own_origin(self, url: str, origin: str) -> None:
        with client().websocket_connect(url, headers={**COOKIE, "Origin": origin}) as ws:
            assert ws.receive_text() == "hello"

    def test_handshakes_without_origin_arent_from_a_browser(self) -> None:
        with client().websocket_connect("/ws", headers=COOKIE) as ws:
            assert ws.receive_text() == "hello"

    def test_fetch_metadata(self) -> None:
        same = {**COOKIE, "Sec-Fetch-Site": "same-origin", "Origin": OWN}
        with client().websocket_connect("/ws", headers=same) as ws:
            assert ws.receive_text() == "hello"
        cross = {**COOKIE, "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "websocket"}
        with pytest.raises(WebSocketDisconnect), client().websocket_connect("/ws", headers=cross):
            pass  # pragma: no cover

    def test_exempt_paths(self) -> None:
        app = client(exempt_paths=["/public"])
        with app.websocket_connect("/public/ws", headers={**COOKIE, "Origin": EVIL}) as ws:
            assert ws.receive_text() == "public"

    async def test_a_client_gone_before_connecting(self) -> None:
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "websocket.disconnect", "code": 1006}

        async def send(message: Message) -> None:
            sent.append(message)

        scope = {
            "type": "websocket",
            "path": "/ws",
            "root_path": "",
            "scheme": "ws",
            "headers": [(b"cookie", b"a=1"), (b"origin", EVIL.encode())],
        }
        await CSRFMiddleware(make_app())(scope, receive, send)
        assert sent == []


class TestOtherScopes:
    def test_lifespan_goes_through(self) -> None:
        with client() as app:
            assert app.get("/items").status_code == 200


class TestConfiguration:
    @pytest.mark.parametrize(
        "origin", ["example.com", "ftp://example.com", "https://example.com/path", "https://"]
    )
    def test_invalid_trusted_origins(self, origin: str) -> None:
        with pytest.raises(ValueError, match="trusted origins"):
            CSRFMiddleware(make_app(), trusted_origins=[origin])

    def test_invalid_exempt_paths(self) -> None:
        with pytest.raises(ValueError, match="start with /"):
            CSRFMiddleware(make_app(), exempt_paths=["webhooks"])

    def test_invalid_header_name(self) -> None:
        with pytest.raises(ValueError, match="header name"):
            CSRFMiddleware(make_app(), header_name="X CSRF")

    def test_host_prefixed_cookies_must_be_secure(self) -> None:
        with pytest.raises(ValueError, match="must be Secure"):
            CSRFMiddleware(make_app(), require_token=True, cookie_secure=False)
        CSRFMiddleware(make_app(), cookie_secure=False)  # no token: no cookie

    @pytest.mark.parametrize(
        ("url", "strict", "expected"),
        [
            ("https://Example.com:443", True, "https://example.com"),
            ("http://example.com:80", True, "http://example.com"),
            ("http://example.com:8080", True, "http://example.com:8080"),
            ("https://[2001:db8::1]:443", True, "https://[2001:db8::1]"),
            ("https://example.com/a?b#c", False, "https://example.com"),
            ("https://example.com/a", True, None),
            ("https://user@example.com", False, None),
            ("https://example.com:99999", False, None),
            ("https://[::1", False, None),
            ("javascript:alert(1)", False, None),
        ],
    )
    def test_origins(self, url: str, strict: bool, expected: str | None) -> None:
        assert _origin(url, strict=strict) == expected


async def run(app: FastAPI, headers: list[tuple[bytes, bytes]]) -> int:
    """The status of the answer to a POST /items with exactly these headers."""
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/items",
        "raw_path": b"/items",
        "root_path": "",
        "scheme": "https",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
    }
    statuses: list[int] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    await app(scope, receive, send)
    return statuses[0]
