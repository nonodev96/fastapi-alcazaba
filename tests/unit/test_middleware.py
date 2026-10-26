"""SecurityHeadersMiddleware and BodySizeLimitMiddleware."""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.types import Message, Receive, Scope, Send

from fastapi_alcazaba.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware

DEFAULTS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
}
API_CSP = "default-src 'none'; frame-ancestors 'none'"


def secured(**options: Any) -> FastAPI:
    app = FastAPI()

    @app.get("/json")
    async def json_response() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/html", response_class=HTMLResponse)
    async def html() -> str:
        return "<p>hi</p>"

    @app.get("/own")
    async def own() -> JSONResponse:
        headers = {"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "default-src 'self'"}
        return JSONResponse({"ok": True}, headers=headers)

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            for chunk in (b"a", b"b", b"c"):
                yield chunk

        return StreamingResponse(chunks(), media_type="text/plain")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    app.add_middleware(SecurityHeadersMiddleware, **options)
    return app


def collect(messages: list[Message]) -> Send:
    async def send(message: Message) -> None:
        messages.append(message)

    return send


def body_of(*chunks: bytes) -> Receive:
    """A receive() that sends `chunks` as the request body, then a disconnect."""
    queue: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]

    async def receive() -> Message:
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    return receive


def http_scope(
    path: str = "/",
    headers: list[tuple[bytes, bytes]] | None = None,
    **extra: Any,
) -> Scope:
    return {"type": "http", "method": "POST", "path": path, "headers": headers or [], **extra}


class TestSecurityHeaders:
    def test_defaults(self) -> None:
        response = TestClient(secured()).get("/json")
        assert {name: response.headers[name] for name in DEFAULTS} == DEFAULTS
        assert response.headers["content-security-policy"] == API_CSP
        assert "strict-transport-security" not in response.headers  # plain HTTP

    def test_the_apps_own_headers_win(self) -> None:
        response = TestClient(secured()).get("/own")
        assert response.headers.get_list("x-frame-options") == ["SAMEORIGIN"]
        assert response.headers.get_list("content-security-policy") == ["default-src 'self'"]
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_hsts_only_over_https(self) -> None:
        https = TestClient(secured(), base_url="https://testserver")
        hsts = "max-age=63072000; includeSubDomains"
        assert https.get("/json").headers["strict-transport-security"] == hsts
        custom = TestClient(secured(hsts="max-age=60"), base_url="https://testserver")
        assert custom.get("/json").headers["strict-transport-security"] == "max-age=60"
        off = TestClient(secured(hsts=None), base_url="https://testserver")
        assert "strict-transport-security" not in off.get("/json").headers

    def test_html_gets_its_own_policy(self) -> None:
        """None by default: the Swagger UI and ReDoc load scripts from a CDN."""
        client = TestClient(secured())
        assert "content-security-policy" not in client.get("/html").headers
        assert "content-security-policy" not in client.get("/docs").headers
        assert client.get("/docs").headers["x-frame-options"] == "DENY"
        strict = TestClient(secured(html_csp="default-src 'self'"))
        assert strict.get("/html").headers["content-security-policy"] == "default-src 'self'"
        assert strict.get("/docs").headers["content-security-policy"] == "default-src 'self'"
        assert strict.get("/json").headers["content-security-policy"] == API_CSP

    def test_without_csp(self) -> None:
        assert "content-security-policy" not in TestClient(secured(csp=None)).get("/json").headers

    def test_more_other_and_fewer_headers(self) -> None:
        headers = {
            "Referrer-Policy": "strict-origin",
            "X-Frame-Options": None,
            "Permissions-Policy": "camera=()",
        }
        response = TestClient(secured(headers=headers)).get("/json")
        assert response.headers["referrer-policy"] == "strict-origin"
        assert "x-frame-options" not in response.headers
        assert response.headers["permissions-policy"] == "camera=()"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_streaming_responses_still_stream(self) -> None:
        response = TestClient(secured()).get("/stream")
        assert (response.text, response.headers["x-content-type-options"]) == ("abc", "nosniff")

    async def test_body_messages_go_through_one_by_one(self) -> None:
        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await send({"type": "http.response.start", "status": 200})  # no headers at all
            for chunk in (b"a", b"b"):
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})

        messages: list[Message] = []
        await SecurityHeadersMiddleware(app)(http_scope(), body_of(b""), collect(messages))
        assert [message["type"] for message in messages] == ["http.response.start"] + [
            "http.response.body"
        ] * 3
        headers = dict(messages[0]["headers"])
        assert headers[b"content-security-policy"] == API_CSP.encode()  # no content type: not HTML
        assert headers[b"x-frame-options"] == b"DENY"

    def test_websockets_and_lifespan_go_through(self) -> None:
        with TestClient(secured()) as client, client.websocket_connect("/ws") as websocket:
            assert websocket.receive_text() == "hello"

    @pytest.mark.parametrize(
        "options",
        [
            {"headers": {"X-Evil": "a\r\nSet-Cookie: session=stolen"}},
            {"headers": {"Bad Name": "x"}},
            {"headers": {"X-Null": "a\0b"}},
            {"hsts": "max-age=1\n"},
            {"csp": "default-src 'none'\r\n"},
            {"html_csp": "\ndefault-src 'self'"},
        ],
    )
    def test_headers_that_would_split(self, options: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="invalid"):
            SecurityHeadersMiddleware(secured(), **options)

    @pytest.mark.parametrize("name", ["Content-Security-Policy", "strict-transport-security"])
    def test_csp_and_hsts_have_their_own_arguments(self, name: str) -> None:
        with pytest.raises(ValueError, match="hsts, csp or html_csp"):
            SecurityHeadersMiddleware(secured(), headers={name: "x"})


class Item(BaseModel):
    name: str


@pytest.fixture(name="calls")
def fixture_calls() -> list[str]:
    return []


@pytest.fixture(name="limited")
def fixture_limited(calls: list[str]) -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        calls.append("echo")
        return {"size": len(await request.body())}

    @app.post("/items")
    async def items(item: Item) -> Item:
        calls.append("items")
        return item

    for path in ("/uploads/file", "/uploadsfoo", "/api/small", "/api/files/x", "/stream"):
        app.post(path)(echo)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    app.add_middleware(
        BodySizeLimitMiddleware,
        max_size=16,
        paths={"/uploads": 1_000, "/stream": None, "/api": 5, "/api/files/": 100},
    )
    return app


def chunked(*parts: bytes) -> Iterator[bytes]:
    """A body without Content-Length: httpx sends it chunked."""
    yield from parts


TOO_LARGE = {"detail": "Request body too large"}


class TestBodySizeLimit:
    def test_a_content_length_over_the_limit(self, limited: FastAPI, calls: list[str]) -> None:
        response = TestClient(limited).post("/echo", content=b"x" * 17)
        assert (response.status_code, response.json()) == (413, TOO_LARGE)
        assert calls == []

    def test_a_chunked_body_over_the_limit(self, limited: FastAPI) -> None:
        response = TestClient(limited).post("/echo", content=chunked(b"x" * 9, b"x" * 9))
        assert (response.status_code, response.json()) == (413, TOO_LARGE)

    def test_fastapi_doesnt_turn_it_into_a_400(self, limited: FastAPI, calls: list[str]) -> None:
        """FastAPI answers 400 to any error while it parses a body, except HTTPExceptions."""
        body = chunked(b'{"name": ', b'"far too long"}')
        response = TestClient(limited).post("/items", content=body)
        assert (response.status_code, response.json()) == (413, TOO_LARGE)
        assert calls == []

    def test_bodies_within_the_limit(self, limited: FastAPI) -> None:
        client = TestClient(limited)
        assert client.post("/echo", content=b"x" * 10).json() == {"size": 10}
        assert client.post("/echo", content=chunked(b"x" * 5, b"x" * 5)).json() == {"size": 10}
        assert client.post("/items", json={"name": "a"}).json() == {"name": "a"}

    @pytest.mark.parametrize(
        ("path", "size", "status"),
        [
            ("/uploads/file", 1_000, 200),
            ("/uploads/file", 1_001, 413),
            ("/uploadsfoo", 17, 413),  # a prefix ends at a /
            ("/stream", 100_000, 200),  # no limit
            ("/api/small", 6, 413),
            ("/api/files/x", 100, 200),  # the longest prefix wins
        ],
    )
    def test_limits_by_path(self, limited: FastAPI, path: str, size: int, status: int) -> None:
        assert TestClient(limited).post(path, content=b"x" * size).status_code == status
        body = chunked(*[b"x"] * size)
        assert TestClient(limited).post(path, content=body).status_code == status

    def test_websockets_and_lifespan_go_through(self, limited: FastAPI) -> None:
        with TestClient(limited) as client, client.websocket_connect("/ws") as websocket:
            websocket.send_text("x" * 100)
            assert websocket.receive_text() == "x" * 100

    def test_limits_for_every_path(self) -> None:
        middleware = BodySizeLimitMiddleware(FastAPI(), max_size=10, paths={"/": None, "/a": 1})
        assert (middleware.limit("/anything"), middleware.limit("/a/b")) == (None, 1)


class TestBodySizeLimitInASGI:
    """What TestClient can't send, and apps without FastAPI's exception handling."""

    @staticmethod
    async def reader(scope: Scope, receive: Receive, send: Send) -> None:
        """Reads the whole body before answering, without handling exceptions."""
        size = 0
        while True:
            message = await receive()
            size += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(size).encode()})

    @pytest.mark.parametrize(
        "lengths", [[b"abc"], [b"+5"], [b" 5"], [b"1_0"], [b"-1"], [b"5", b"6"]]
    )
    async def test_invalid_content_length(self, lengths: list[bytes]) -> None:
        messages: list[Message] = []
        headers = [(b"content-length", length) for length in lengths]
        middleware = BodySizeLimitMiddleware(self.reader, max_size=10)
        await middleware(http_scope(headers=headers), body_of(b"x"), collect(messages))
        assert messages[0]["status"] == 400
        assert messages[1]["body"] == b'{"detail":"Invalid Content-Length"}'

    @pytest.mark.parametrize(
        ("length", "status"),
        [
            (b"0" * 5_000 + b"1", 200),  # leading zeros: 1
            (b"0" * 5_000, 200),
            (b"1" + b"0" * 5_000, 413),  # past int()'s limit of 4300 digits
            (b"0" * 10 + b"11", 413),
        ],
    )
    async def test_content_lengths_with_thousands_of_digits(
        self,
        length: bytes,
        status: int,
    ) -> None:
        """httptools accepts them; int() would raise on more than 4300 digits."""
        messages: list[Message] = []
        headers = [(b"content-length", length)]
        middleware = BodySizeLimitMiddleware(self.reader, max_size=10)
        await middleware(http_scope(headers=headers), body_of(b"x"), collect(messages))
        assert messages[0]["status"] == status

    async def test_the_same_content_length_twice(self) -> None:
        messages: list[Message] = []
        headers = [(b"content-length", b"1"), (b"Content-Length", b"1")]
        middleware = BodySizeLimitMiddleware(self.reader, max_size=10)
        await middleware(http_scope(headers=headers), body_of(b"x"), collect(messages))
        assert (messages[0]["status"], messages[1]["body"]) == (200, b"1")

    async def test_more_than_the_content_length_said(self) -> None:
        messages: list[Message] = []
        headers = [(b"content-length", b"5")]
        middleware = BodySizeLimitMiddleware(self.reader, max_size=10)
        await middleware(
            http_scope(headers=headers), body_of(b"x" * 8, b"x" * 8), collect(messages)
        )
        assert messages[0]["status"] == 413
        assert messages[0]["headers"] == [
            (b"content-type", b"application/json"),
            (b"content-length", b"35"),
        ]
        assert messages[1]["body"] == b'{"detail":"Request body too large"}'

    async def test_too_late_to_answer(self) -> None:
        async def streams_back(scope: Scope, receive: Receive, send: Send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            while (await receive()).get("more_body"):
                await send({"type": "http.response.body", "body": b".", "more_body": True})

        messages: list[Message] = []
        middleware = BodySizeLimitMiddleware(streams_back, max_size=10)
        with pytest.raises(HTTPException) as exc_info:
            await middleware(http_scope(), body_of(b"x" * 6, b"x" * 6), collect(messages))
        assert exc_info.value.status_code == 413
        assert messages[0]["status"] == 200  # sent before the body went over the limit

    async def test_disconnects_go_through(self) -> None:
        received: list[Message] = []

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            received.extend([await receive(), await receive()])

        await BodySizeLimitMiddleware(app, max_size=10)(http_scope(), body_of(b"x"), collect([]))
        assert [message["type"] for message in received] == ["http.request", "http.disconnect"]

    @pytest.mark.parametrize(
        ("path", "root_path", "status"),
        [
            ("/prefix/uploads/a", "/prefix", 200),  # routed as /uploads/a
            ("/prefix", "/prefix", 413),  # routed as /
            ("/prefixed/uploads/a", "/prefix", 413),  # not under the root path
            ("/uploads/a", "/prefix", 200),  # a server that leaves the root path out of path
        ],
    )
    async def test_paths_without_the_root_path(
        self,
        path: str,
        root_path: str,
        status: int,
    ) -> None:
        messages: list[Message] = []
        middleware = BodySizeLimitMiddleware(self.reader, max_size=1, paths={"/uploads": 100})
        scope = http_scope(path, root_path=root_path)
        await middleware(scope, body_of(b"x" * 50), collect(messages))
        assert messages[0]["status"] == status

    @pytest.mark.parametrize(
        ("options", "match"),
        [
            ({"max_size": -1}, "max_size"),
            ({"paths": {"uploads": 10}}, "start with /"),
            ({"paths": {"/uploads": -1}}, "negative"),
        ],
    )
    def test_validation(self, options: dict[str, Any], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            BodySizeLimitMiddleware(FastAPI(), **options)
