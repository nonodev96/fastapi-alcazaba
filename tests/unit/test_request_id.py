"""RequestIDMiddleware, request_id() and RequestIDLogFilter."""

import asyncio
import logging
import re
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.types import Message, Receive, Scope, Send

from fastapi_alcazaba.request_id import RequestIDLogFilter, RequestIDMiddleware, request_id

GENERATED = re.compile(r"[0-9a-f]{32}")


def make_app(**options: Any) -> FastAPI:
    app = FastAPI()

    @app.get("/id")
    async def current() -> dict[str, str | None]:
        return {"id": request_id()}

    @app.get("/sync")
    def current_in_a_thread() -> dict[str, str | None]:
        return {"id": request_id()}

    @app.get("/own")
    async def own() -> JSONResponse:
        return JSONResponse({}, headers={"X-Request-ID": "set-by-the-app"})

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(request_id() or "")
        await websocket.close()

    @app.websocket("/ws-denied")
    async def ws_denied(websocket: WebSocket) -> None:
        raise HTTPException(403)

    app.add_middleware(RequestIDMiddleware, **options)
    return app


def collect(messages: list[Message]) -> Send:
    async def send(message: Message) -> None:
        messages.append(message)

    return send


async def no_body() -> Message:
    return {"type": "http.request", "body": b""}


def http_scope(headers: list[tuple[bytes, bytes]]) -> Scope:
    return {"type": "http", "method": "GET", "path": "/", "headers": headers}


async def seen_id(header_values: list[bytes], **options: Any) -> tuple[str | None, bytes]:
    """The ID the app sees for a request with these X-Request-ID values, and the response's."""
    seen: list[str | None] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(request_id())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages: list[Message] = []
    headers = [(b"x-request-id", value) for value in header_values]
    await RequestIDMiddleware(app, **options)(http_scope(headers), no_body, collect(messages))
    return seen[0], dict(messages[0]["headers"])[b"x-request-id"]


class TestRequestID:
    def test_a_new_id_for_every_request(self) -> None:
        client = TestClient(make_app())
        first, second = client.get("/id"), client.get("/id")
        assert GENERATED.fullmatch(first.headers["x-request-id"])
        assert first.json() == {"id": first.headers["x-request-id"]}
        assert first.headers["x-request-id"] != second.headers["x-request-id"]

    def test_sync_endpoints_see_it_in_their_thread(self) -> None:
        response = TestClient(make_app()).get("/sync")
        assert response.json() == {"id": response.headers["x-request-id"]}

    @pytest.mark.parametrize(
        "incoming",
        ["abc-123", "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", "a" * 128],
    )
    def test_a_safe_incoming_id_is_reused(self, incoming: str) -> None:
        response = TestClient(make_app()).get("/id", headers={"X-Request-ID": incoming})
        assert (response.headers["x-request-id"], response.json()) == (incoming, {"id": incoming})

    @pytest.mark.parametrize(
        "incoming",
        [
            b"abc\r\nINFO forged log line",
            b"abc\nx",
            b"has spaces",
            b"a" * 129,
            b"",
            b"caf\xc3\xa9",
            b"{json}",
        ],
    )
    async def test_an_unsafe_incoming_id_is_replaced(self, incoming: bytes) -> None:
        seen, sent = await seen_id([incoming])
        assert seen is not None
        assert GENERATED.fullmatch(seen)
        assert sent == seen.encode()

    async def test_several_incoming_ids_are_ambiguous(self) -> None:
        seen, _ = await seen_id([b"one", b"two"])
        assert seen is not None
        assert GENERATED.fullmatch(seen)

    async def test_incoming_ids_can_be_ignored(self) -> None:
        seen, _ = await seen_id([b"chosen-by-the-client"], trust_incoming=False)
        assert seen is not None
        assert GENERATED.fullmatch(seen)

    def test_another_header(self) -> None:
        client = TestClient(make_app(header="X-Correlation-ID"))
        response = client.get("/id", headers={"X-Correlation-ID": "abc"})
        assert (response.headers["x-correlation-id"], response.json()) == ("abc", {"id": "abc"})
        assert "x-request-id" not in response.headers

    @pytest.mark.parametrize("header", ["X Request", "X-Request-ID\r\n", ""])
    def test_invalid_header_names(self, header: str) -> None:
        with pytest.raises(ValueError, match="invalid header name"):
            RequestIDMiddleware(FastAPI(), header=header)

    def test_the_apps_own_header_wins(self) -> None:
        response = TestClient(make_app()).get("/own")
        assert response.headers.get_list("x-request-id") == ["set-by-the-app"]

    def test_websockets(self) -> None:
        with TestClient(make_app()) as client:
            headers = {"X-Request-ID": "ws-1"}
            with client.websocket_connect("/ws", headers=headers) as websocket:
                assert websocket.receive_text() == "ws-1"
                assert (b"x-request-id", b"ws-1") in (websocket.extra_headers or [])

    def test_websocket_denials(self) -> None:
        client = TestClient(make_app())
        with pytest.raises(WebSocketDenialResponse) as exc_info:
            with client.websocket_connect("/ws-denied", headers={"X-Request-ID": "ws-2"}):
                pass  # pragma: no cover
        assert exc_info.value.status_code == 403
        assert exc_info.value.headers["x-request-id"] == "ws-2"

    async def test_lifespan_goes_through(self) -> None:
        scopes: list[str] = []

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            scopes.append(scope["type"])

        await RequestIDMiddleware(app)({"type": "lifespan"}, no_body, collect([]))
        assert scopes == ["lifespan"]


class TestRequestIDContext:
    def test_none_outside_a_request(self) -> None:
        assert request_id() is None

    async def test_reset_after_the_request(self) -> None:
        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        await RequestIDMiddleware(app)(http_scope([]), no_body, collect([]))
        assert request_id() is None

    async def test_kept_after_an_exception(self) -> None:
        """The error handler and the server's log run outside the middleware, in its task."""

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            raise RuntimeError("boom")

        async def request() -> str | None:
            with pytest.raises(RuntimeError):
                await RequestIDMiddleware(app)(
                    http_scope([(b"x-request-id", b"failed")]), no_body, collect([])
                )
            return request_id()

        # A task of its own, as servers give every request: it doesn't leak into this one.
        assert await asyncio.create_task(request()) == "failed"
        assert request_id() is None

    async def test_concurrent_requests_dont_mix(self) -> None:
        seen: dict[bytes, str | None] = {}

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await asyncio.sleep(0.01)
            seen[scope["headers"][0][1]] = request_id()

        middleware = RequestIDMiddleware(app)
        await asyncio.gather(
            *(
                middleware(http_scope([(b"x-request-id", name)]), no_body, collect([]))
                for name in (b"a", b"b", b"c")
            )
        )
        assert seen == {b"a": "a", b"b": "b", b"c": "c"}


def record() -> logging.LogRecord:
    return logging.LogRecord("app", logging.INFO, __file__, 1, "message", None, None)


class TestRequestIDLogFilter:
    def test_outside_a_request(self) -> None:
        entry = record()
        assert RequestIDLogFilter().filter(entry) is True
        assert entry.__dict__["request_id"] == "-"
        RequestIDLogFilter(default="none").filter(entry := record())
        assert entry.__dict__["request_id"] == "none"

    def test_an_extra_request_id_is_kept(self) -> None:
        entry = record()
        entry.__dict__["request_id"] = "given"
        RequestIDLogFilter().filter(entry)
        assert entry.__dict__["request_id"] == "given"

    def test_in_a_request(self) -> None:
        app = FastAPI()
        formatted: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                formatted.append(self.format(record))

        handler = Collect()
        handler.addFilter(RequestIDLogFilter())
        handler.setFormatter(logging.Formatter("[%(request_id)s] %(message)s"))
        logger = logging.getLogger("tests.request_id")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        @app.get("/")
        async def endpoint() -> None:
            logger.info("handling")

        app.add_middleware(RequestIDMiddleware)
        try:
            TestClient(app).get("/", headers={"X-Request-ID": "req-7"})
            logger.info("after")
        finally:
            logger.removeHandler(handler)
        assert formatted == ["[req-7] handling", "[-] after"]
