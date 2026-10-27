"""RequestTimeoutMiddleware: a deadline for every request."""

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from fastapi_alcazaba.observability import RequestIDMiddleware, RequestTimeoutMiddleware

TIMED_OUT = {"detail": "Request timed out"}


def collect(messages: list[Message]) -> Send:
    async def send(message: Message) -> None:
        messages.append(message)

    return send


async def no_body() -> Message:
    return {"type": "http.request", "body": b""}


def http_scope(path: str = "/", **extra: Any) -> Scope:
    return {"type": "http", "method": "GET", "path": path, "headers": [], **extra}


async def answer(send: Send, body: bytes = b"ok") -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


@pytest.fixture(name="events")
def fixture_events() -> list[str]:
    return []


@pytest.fixture(name="release")
def fixture_release() -> Iterator[threading.Event]:
    event = threading.Event()
    yield event
    event.set()  # lets an abandoned sync endpoint finish


@pytest.fixture(name="app")
def fixture_app(events: list[str], release: threading.Event) -> FastAPI:
    app = FastAPI()

    async def resource() -> AsyncIterator[None]:
        try:
            yield
        finally:
            events.append("dependency closed")

    @app.get("/slow")
    async def slow(_: None = Depends(resource)) -> None:
        try:
            await asyncio.sleep(10)
        finally:
            events.append("endpoint cancelled")

    @app.get("/fast")
    async def fast() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/sync")
    def sync() -> None:
        release.wait(10)
        events.append("thread finished")

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"first"
            await asyncio.sleep(10)
            yield b"never"  # pragma: no cover

        return StreamingResponse(chunks(), media_type="text/plain")

    @app.get("/reports/monthly")
    async def report() -> dict[str, bool]:
        await asyncio.sleep(0.1)
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await asyncio.sleep(0.1)
        await websocket.send_text("still here")
        await websocket.close()

    app.add_middleware(RequestTimeoutMiddleware, timeout=0.05, paths={"/reports": 1})
    return app


class TestRequestTimeout:
    def test_a_slow_request_gets_a_504(
        self,
        app: FastAPI,
        events: list[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.observability.timeouts"):
            response = TestClient(app).get("/slow")
        assert (response.status_code, response.json()) == (504, TIMED_OUT)
        assert response.headers["content-type"] == "application/json"
        assert events == ["endpoint cancelled", "dependency closed"]
        assert "GET /slow timed out after 0.05s: answered 504" in caplog.text

    def test_fast_requests_go_through(self, app: FastAPI) -> None:
        assert TestClient(app).get("/fast").json() == {"ok": True}

    def test_other_deadlines_by_path(self, app: FastAPI) -> None:
        assert TestClient(app).get("/reports/monthly").json() == {"ok": True}

    def test_sync_endpoints_get_their_504_on_time(
        self,
        app: FastAPI,
        events: list[str],
        release: threading.Event,
    ) -> None:
        start = time.monotonic()
        response = TestClient(app).get("/sync")
        assert response.status_code == 504
        assert time.monotonic() - start < 5
        assert events == []  # the thread can't be interrupted: it's still running
        release.set()

    def test_a_started_response_is_cut(
        self,
        app: FastAPI,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            caplog.at_level(logging.WARNING, "fastapi_alcazaba.observability.timeouts"),
            pytest.raises(TimeoutError),
        ):
            TestClient(app).get("/stream")
        assert "GET /stream timed out after 0.05s with its response under way" in caplog.text

    def test_websockets_go_through(self, app: FastAPI) -> None:
        with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
            assert websocket.receive_text() == "still here"

    def test_with_request_ids(self, app: FastAPI) -> None:
        app.add_middleware(RequestIDMiddleware)
        response = TestClient(app).get("/slow", headers={"X-Request-ID": "slow-1"})
        assert (response.status_code, response.headers["x-request-id"]) == (504, "slow-1")


class TestRequestTimeoutInASGI:
    async def test_a_cut_response_ends_with_an_exception(self) -> None:
        """The server closes the connection: the client never sees a complete-looking body."""

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"a", "more_body": True})
            await asyncio.sleep(10)

        messages: list[Message] = []
        with pytest.raises(TimeoutError):
            await RequestTimeoutMiddleware(app, timeout=0.01)(
                http_scope(), no_body, collect(messages)
            )
        assert [message.get("more_body") for message in messages] == [None, True]

    async def test_the_apps_own_timeout_errors(self) -> None:
        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            raise TimeoutError("the app's")

        messages: list[Message] = []
        with pytest.raises(TimeoutError, match="the app's"):
            await RequestTimeoutMiddleware(app, timeout=10)(
                http_scope(), no_body, collect(messages)
            )
        assert messages == []

    async def test_an_app_that_swallows_the_cancellation(self) -> None:
        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass  # and returns without answering

        messages: list[Message] = []
        await RequestTimeoutMiddleware(app, timeout=0.01)(http_scope(), no_body, collect(messages))
        assert messages[0]["status"] == 504
        assert messages[1]["body"] == b'{"detail":"Request timed out"}'

    async def test_an_app_that_swallows_it_and_answers(self) -> None:
        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await answer(send, b"late")

        messages: list[Message] = []
        await RequestTimeoutMiddleware(app, timeout=0.01)(http_scope(), no_body, collect(messages))
        assert [messages[0]["status"], messages[1]["body"]] == [200, b"late"]

    async def test_no_deadline_once_the_response_is_out(self) -> None:
        """Background tasks run after the response, as long as they need."""
        done: list[bool] = []

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"a", "more_body": True})
            await send({"type": "http.response.body", "body": b"b"})
            await asyncio.sleep(0.05)
            done.append(True)

        messages: list[Message] = []
        await RequestTimeoutMiddleware(app, timeout=0.02)(http_scope(), no_body, collect(messages))
        assert done == [True]
        assert len(messages) == 3

    async def test_a_slow_body_counts(self) -> None:
        """A client that trickles its body runs out of time too."""

        async def trickle() -> Message:
            await asyncio.sleep(10)
            return {"type": "http.request", "body": b""}  # pragma: no cover

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await receive()

        messages: list[Message] = []
        await RequestTimeoutMiddleware(app, timeout=0.01)(http_scope(), trickle, collect(messages))
        assert messages[0]["status"] == 504

    async def test_lifespan_goes_through(self) -> None:
        scopes: list[str] = []

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await asyncio.sleep(0.03)
            scopes.append(scope["type"])

        await RequestTimeoutMiddleware(app, timeout=0.01)(
            {"type": "lifespan"}, no_body, collect([])
        )
        assert scopes == ["lifespan"]

    @pytest.mark.parametrize(
        ("path", "root_path", "status"),
        [
            ("/prefix/exports/a", "/prefix", 200),  # routed as /exports/a: no deadline
            ("/prefix", "/prefix", 504),  # routed as /
            ("/prefixed/exports/a", "/prefix", 504),  # not under the root path
            ("/exports/a", "/prefix", 200),  # a server that leaves the root path out of path
        ],
    )
    async def test_paths_without_the_root_path(
        self,
        path: str,
        root_path: str,
        status: int,
    ) -> None:
        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await asyncio.sleep(0.03)
            await answer(send)

        messages: list[Message] = []
        middleware = RequestTimeoutMiddleware(app, timeout=0.01, paths={"/exports": None})
        await middleware(http_scope(path, root_path=root_path), no_body, collect(messages))
        assert messages[0]["status"] == status

    async def test_logs_quote_paths_that_match_no_route(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await asyncio.sleep(10)

        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.observability.timeouts"):
            await RequestTimeoutMiddleware(app, timeout=0.01)(
                http_scope("/a\nFORGED"), no_body, collect([])
            )
        assert "GET '/a\\nFORGED' timed out" in caplog.text
        assert "\nFORGED" not in caplog.text


class TestDeadlines:
    def test_the_longest_prefix_wins(self) -> None:
        middleware = RequestTimeoutMiddleware(
            FastAPI(), timeout=5, paths={"/api": 10, "/api/exports/": None, "/": 1}
        )
        assert middleware.timeout_for("/api/items") == 10
        assert middleware.timeout_for("/api/exports/x") is None
        assert middleware.timeout_for("/api/exports") is None
        assert middleware.timeout_for("/apiary") == 1
        assert middleware.timeout_for("/other") == 1

    def test_no_default_deadline(self) -> None:
        middleware = RequestTimeoutMiddleware(FastAPI(), timeout=None, paths={"/api": 2})
        assert (middleware.timeout_for("/api/x"), middleware.timeout_for("/x")) == (2, None)

    @pytest.mark.parametrize(
        ("options", "match"),
        [
            ({"timeout": 0}, "timeout must be positive"),
            ({"timeout": -1}, "timeout must be positive"),
            ({"timeout": float("nan")}, "timeout must be positive"),
            ({"paths": {"api": 1}}, "start with /"),
            ({"paths": {"/api": 0}}, "must be positive"),
        ],
    )
    def test_validation(self, options: dict[str, Any], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            RequestTimeoutMiddleware(FastAPI(), **options)
