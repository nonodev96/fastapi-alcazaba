"""Idempotency-Key: a retried POST gets the first response again and runs once."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI, Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.types import Message

from fastapi_alcazaba import BackendUnavailable, Cache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.idempotency import IdempotencyMiddleware, require_idempotency_key
from tests.memory import state

KEY = {"Idempotency-Key": '"8e03978e-40d5-43e8-bc93-6894a57f9324"'}


class Order(BaseModel):
    item: str


class App:
    """An app whose POST /orders counts how many times it really ran."""

    def __init__(self, backend: InMemoryBackend | None = None, **options: Any) -> None:
        self.backend = backend if backend is not None else InMemoryBackend()
        self.runs = 0
        self.gate: asyncio.Event | None = None
        self.app = FastAPI()
        self.cache = Cache(self.backend, prefix="app", **options.pop("cache_options", {}))
        self.cache.install(self.app)
        self.app.add_middleware(IdempotencyMiddleware, **options)

        @self.app.post("/orders", status_code=201)
        async def create(order: Order, response: Response) -> dict[str, Any]:
            self.runs += 1
            if self.gate is not None:
                await self.gate.wait()
            response.headers["Location"] = f"/orders/{self.runs}"
            return {"id": self.runs, "item": order.item}

        @self.app.post("/fail")
        async def fail(status: int) -> Response:
            self.runs += 1
            return Response(status_code=status)

        @self.app.post("/boom")
        async def boom() -> None:
            self.runs += 1
            raise RuntimeError("boom")

        @self.app.post("/unavailable")
        async def unavailable() -> None:
            self.runs += 1
            raise BackendUnavailable("the app's own backend")

        @self.app.post("/stream")
        async def stream() -> StreamingResponse:
            self.runs += 1

            async def chunks() -> AsyncIterator[bytes]:
                for chunk in (b"a", b"b", b"c"):
                    yield chunk

            return StreamingResponse(chunks(), media_type="text/plain")

        @self.app.get("/orders")
        async def listing() -> int:
            self.runs += 1
            return self.runs

        @self.app.post("/payments", dependencies=[Depends(require_idempotency_key)])
        async def pay() -> dict[str, bool]:
            self.runs += 1
            return {"paid": True}

    def client(self, **kwargs: Any) -> TestClient:
        return TestClient(self.app, raise_server_exceptions=False, **kwargs)


class TestRetries:
    def test_a_retry_gets_the_first_response(self) -> None:
        app = App()
        client = app.client()
        first = client.post("/orders", json={"item": "olive oil"}, headers=KEY)
        retry = client.post("/orders", json={"item": "olive oil"}, headers=KEY)
        assert (first.status_code, first.json()) == (201, {"id": 1, "item": "olive oil"})
        assert (retry.status_code, retry.json()) == (201, first.json())
        assert retry.headers["location"] == "/orders/1"
        assert retry.headers["idempotent-replayed"] == "true"
        assert "idempotent-replayed" not in first.headers
        assert app.runs == 1

    def test_a_retry_gets_no_cookies(self) -> None:
        """A replay never hands out a session: whoever sends it may not be who logged in."""
        app = App()

        @app.app.post("/sessions")
        async def log_in(response: Response) -> dict[str, bool]:
            app.runs += 1
            response.set_cookie("session", "s3cret")
            return {"logged_in": True}

        client = app.client()
        assert client.post("/sessions", headers=KEY).headers["set-cookie"].startswith("session=")
        retry = client.post("/sessions", headers=KEY)
        assert retry.json() == {"logged_in": True}
        assert "set-cookie" not in retry.headers
        assert app.runs == 1

    def test_quoted_and_bare_keys_are_the_same_key(self) -> None:
        app = App()
        client = app.client()
        client.post("/orders", json={"item": "a"}, headers={"Idempotency-Key": '"k\\"1"'})
        retry = client.post("/orders", json={"item": "a"}, headers={"Idempotency-Key": 'k"1'})
        assert retry.headers["idempotent-replayed"] == "true"
        assert app.runs == 1

    def test_without_the_header_every_request_runs(self) -> None:
        app = App()
        client = app.client()
        for _ in range(2):
            assert client.post("/orders", json={"item": "a"}).status_code == 201
        assert app.runs == 2

    def test_other_methods_go_through(self) -> None:
        app = App()
        client = app.client()
        assert [client.get("/orders", headers=KEY).json() for _ in range(2)] == [1, 2]

    def test_streaming_responses(self) -> None:
        app = App()
        client = app.client()
        assert client.post("/stream", headers=KEY).text == "abc"
        retry = client.post("/stream", headers=KEY)
        assert (retry.text, retry.headers["content-type"]) == ("abc", "text/plain; charset=utf-8")
        assert app.runs == 1

    def test_records_expire(self) -> None:
        app = App(expire=120)
        app.client().post("/orders", json={"item": "a"}, headers=KEY)
        (record,) = (value for value in app.backend._state["idempotency"].values())
        assert record.expires_at is not None


class TestRefusals:
    def test_the_same_key_for_another_request(self) -> None:
        app = App()
        client = app.client()
        client.post("/orders", json={"item": "a"}, headers=KEY)
        for path, body in (("/orders", {"item": "b"}), ("/orders?x=1", {"item": "a"})):
            refused = client.post(path, json=body, headers=KEY)
            assert (refused.status_code, refused.json()) == (
                422,
                {"detail": "This Idempotency-Key was used with a different request"},
            )
        assert app.runs == 1

    @pytest.mark.parametrize("key", [b"", b'""', b"x" * 256, "é".encode("latin-1"), b"\x01"])
    def test_invalid_keys(self, key: bytes) -> None:
        app = App()
        headers = {b"Idempotency-Key": key}
        refused = app.client().post("/orders", json={"item": "a"}, headers=headers)
        assert (refused.status_code, refused.json()) == (
            400,
            {"detail": "Invalid Idempotency-Key header"},
        )
        assert app.runs == 0

    def test_bodies_too_large_to_fingerprint(self) -> None:
        app = App(max_body_size=10)
        refused = app.client().post("/orders", json={"item": "a" * 20}, headers=KEY)
        assert (refused.status_code, app.runs) == (413, 0)

    async def test_a_retry_while_the_first_one_runs(self) -> None:
        app = App()
        app.gate = asyncio.Event()
        transport = httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(client.post("/orders", json={"item": "a"}, headers=KEY))
            while app.runs == 0:
                await asyncio.sleep(0.01)
            retry = await client.post("/orders", json={"item": "a"}, headers=KEY)
            assert (retry.status_code, retry.headers["retry-after"]) == (409, "1")
            app.gate.set()
            assert (await first).status_code == 201
            later = await client.post("/orders", json={"item": "a"}, headers=KEY)
        assert later.headers["idempotent-replayed"] == "true"
        assert app.runs == 1

    def test_the_route_can_require_a_key(self) -> None:
        app = App()
        client = app.client()
        refused = client.post("/payments")
        assert (refused.status_code, refused.json()) == (
            400,
            {"detail": "This request needs an Idempotency-Key header"},
        )
        assert client.post("/payments", headers=KEY).json() == {"paid": True}


class TestWhatIsKept:
    @pytest.mark.parametrize("status", [500, 503, 408, 409, 425, 429])
    def test_answers_worth_retrying_arent_kept(self, status: int) -> None:
        app = App()
        client = app.client()
        for _ in range(2):
            assert client.post(f"/fail?status={status}", headers=KEY).status_code == status
        assert app.runs == 2

    @pytest.mark.parametrize("status", [401, 403, 404, 405, 413, 415, 422])
    def test_answers_about_the_request_itself_arent_kept(self, status: int) -> None:
        app = App()
        client = app.client()
        for _ in range(2):
            assert client.post(f"/fail?status={status}", headers=KEY).status_code == status
        assert app.runs == 2

    @pytest.mark.parametrize("status", [400, 402, 410])
    def test_other_client_errors_are_kept(self, status: int) -> None:
        app = App()
        client = app.client()
        for _ in range(2):
            assert client.post(f"/fail?status={status}", headers=KEY).status_code == status
        assert app.runs == 1

    def test_invalid_bodies_arent_kept(self) -> None:
        app = App()
        client = app.client()
        for _ in range(2):
            refused = client.post("/orders", json={"wrong": "x" * 1000}, headers=KEY)
            assert refused.status_code == 422
        assert not [*app.backend._state.get("idempotency", ())]

    def test_an_exception_lets_the_retry_run(self) -> None:
        app = App()
        client = app.client()
        assert [client.post("/boom", headers=KEY).status_code for _ in range(2)] == [500, 500]
        assert app.runs == 2

    def test_responses_too_large_to_keep(self) -> None:
        app = App(max_response_size=10)
        client = app.client()
        for _ in range(2):
            client.post("/orders", json={"item": "a long name"}, headers=KEY)
        assert app.runs == 2


class TestClients:
    def test_each_user_has_its_own_keys(self) -> None:
        app = App()
        client = app.client()
        for user in ("alice", "bob"):
            headers = {**KEY, "Authorization": f"Bearer {user}"}
            assert client.post("/orders", json={"item": "a"}, headers=headers).status_code == 201
        assert app.runs == 2

    def test_a_custom_identifier(self) -> None:
        async def tenant(connection: Any) -> str:
            return str(connection.headers.get("x-tenant", ""))

        app = App(identifier=tenant)
        client = app.client()
        for tenant_id in ("1", "1", "2"):
            client.post("/orders", json={"item": "a"}, headers={**KEY, "X-Tenant": tenant_id})
        assert app.runs == 2

    def test_keys_and_identities_never_reach_the_backend_in_clear(self) -> None:
        app = App()
        app.client().post("/orders", json={"item": "a"}, headers={**KEY, "Authorization": "secret"})
        keys = " ".join([*app.backend._store, *app.backend._state.get("idempotency", ())])
        assert "8e03978e" not in keys
        assert "secret" not in keys


class TestProtectedRecords:
    def test_records_are_sealed(self) -> None:
        app = App(cache_options={"encryption_key": "e" * 32})
        app.client().post("/orders", json={"item": "olive oil"}, headers=KEY)
        (record,) = (value for value in app.backend._state["idempotency"].values())
        assert b"olive" not in record.data

    def test_planted_records_are_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        app = App(cache_options={"signing_key": "s" * 32})
        client = app.client()
        client.post("/orders", json={"item": "a"}, headers=KEY)
        (key,) = (key for key in state(app.backend) if "idempotency" in key)
        forged = {"fingerprint": "x", "status": 200, "headers": [], "body": ""}
        state(app.backend)[key].data = json.dumps(forged).encode()
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.idempotency.middleware"):
            assert client.post("/orders", json={"item": "a"}, headers=KEY).status_code == 201
        assert app.runs == 2
        assert "isn't ours" in caplog.text

    @pytest.mark.parametrize(
        "garbage",
        [
            b"not json",
            b"[]",
            b'{"fingerprint": "x"}',
            b'{"fingerprint": 1, "status": 200, "headers": [], "body": ""}',
            b'{"fingerprint": "x", "status": "200", "headers": [], "body": ""}',
            b'{"fingerprint": "x", "status": 200, "headers": [1], "body": ""}',
            b'{"fingerprint": "x", "status": 200, "headers": [], "body": "not base64!"}',
        ],
    )
    def test_garbage_is_ignored(self, garbage: bytes) -> None:
        app = App()
        client = app.client()
        client.post("/orders", json={"item": "a"}, headers=KEY)
        (key,) = (key for key in state(app.backend) if "idempotency" in key)
        state(app.backend)[key].data = garbage
        assert client.post("/orders", json={"item": "a"}, headers=KEY).status_code == 201
        assert app.runs == 2


class Down(InMemoryBackend):
    def __init__(self, failing: set[str]) -> None:
        super().__init__()
        self.failing = failing

    async def get(self, key: str) -> bytes | None:
        if "get" in self.failing:
            raise ConnectionError("down")
        return await super().get(key)

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        if "set" in self.failing:
            raise ConnectionError("down")
        await super().set(key, value, expire)


class TestFailures:
    def test_a_failing_backend_refuses_by_default(self) -> None:
        app = App(Down({"get"}))
        refused = app.client().post("/orders", json={"item": "a"}, headers=KEY)
        assert (refused.status_code, refused.json()) == (503, {"detail": "Service Unavailable"})
        assert app.runs == 0

    def test_failing_open(self) -> None:
        app = App(Down({"get"}), fail_open=True)
        response = app.client().post("/orders", json={"item": "a"}, headers=KEY)
        assert (response.status_code, app.runs) == (201, 1)

    def test_a_backend_that_fails_after_the_lock(self) -> None:
        backend = Down(set())
        app = App(backend)
        original = backend.acquire_lock

        async def then_fail(key: str, token: str, ttl: float) -> bool:
            backend.failing.add("get")
            return await original(key, token, ttl)

        backend.acquire_lock = then_fail  # type: ignore[method-assign]
        refused = app.client().post("/orders", json={"item": "a"}, headers=KEY)
        assert (refused.status_code, app.runs) == (503, 0)

    def test_failing_open_after_the_lock(self) -> None:
        backend = Down(set())
        app = App(backend, fail_open=True)
        original = backend.acquire_lock

        async def then_fail(key: str, token: str, ttl: float) -> bool:
            backend.failing.add("get")
            return await original(key, token, ttl)

        backend.acquire_lock = then_fail  # type: ignore[method-assign]
        response = app.client().post("/orders", json={"item": "a"}, headers=KEY)
        assert (response.status_code, app.runs) == (201, 1)
        assert not backend._locks  # released

    def test_the_apps_own_backend_failures_are_the_apps(self) -> None:
        """Not answered with the middleware's 503, which could come after the response began."""
        app = App()
        client = app.client()
        for _ in range(2):
            assert client.post("/unavailable", headers=KEY).status_code == 500
        assert app.runs == 2  # not kept, and the lock released

    def test_a_record_stored_while_waiting_for_the_lock(self) -> None:
        """The first request finished between this one's read and its lock: no second run."""

        class LateRecord(InMemoryBackend):
            hide = False

            async def get(self, key: str) -> bytes | None:
                if self.hide and "idempotency" in key:
                    self.hide = False
                    return None
                return await super().get(key)

        backend = LateRecord()
        app = App(backend)
        client = app.client()
        client.post("/orders", json={"item": "a"}, headers=KEY)
        backend.hide = True
        retry = client.post("/orders", json={"item": "a"}, headers=KEY)
        assert (retry.headers["idempotent-replayed"], app.runs) == ("true", 1)

    def test_a_response_that_cant_be_kept(self, caplog: pytest.LogCaptureFixture) -> None:
        app = App(Down({"set"}))
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.idempotency.middleware"):
            response = app.client().post("/orders", json={"item": "a"}, headers=KEY)
        assert response.status_code == 201
        assert "Couldn't keep the response" in caplog.text

    def test_backends_without_locks(self) -> None:
        class WithoutLocks(InMemoryBackend):
            supports_locks = False

        app = App(WithoutLocks())
        client = TestClient(app.app)
        with pytest.raises(NotImplementedError, match="can't hold locks"):
            client.post("/orders", json={"item": "a"}, headers=KEY)


class TestASGI:
    @pytest.mark.parametrize(
        "incoming",
        [
            [{"type": "http.disconnect"}],
            [
                {"type": "http.request", "body": b'{"it', "more_body": True},
                {"type": "http.disconnect"},
            ],
        ],
    )
    async def test_a_client_that_disconnects_while_sending_the_body(
        self,
        incoming: list[Message],
    ) -> None:
        """The app doesn't run with half a body, whose answer a retry with all of it would get."""
        backend = InMemoryBackend()
        ran: list[bool] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            ran.append(True)

        middleware = IdempotencyMiddleware(app, cache=Cache(backend))

        async def receive() -> Message:
            return incoming.pop(0)

        async def send(message: Message) -> None:
            raise AssertionError("nobody to answer")

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "query_string": b"",
            "headers": [(b"idempotency-key", b"k")],
            "app": FastAPI(),
        }
        await middleware(scope, receive, send)
        assert not ran
        assert not state(backend)
        assert not backend._locks

    async def test_a_body_too_large_in_several_chunks(self) -> None:
        """Reading stops once it's past the limit, without waiting for the rest."""
        incoming: list[Message] = [
            {"type": "http.request", "body": b"x" * 6, "more_body": True} for _ in range(100)
        ]
        sent: list[Message] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            raise AssertionError("not run")

        async def receive() -> Message:
            return incoming.pop(0)

        async def send(message: Message) -> None:
            sent.append(message)

        middleware = IdempotencyMiddleware(app, cache=Cache(InMemoryBackend()), max_body_size=10)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "query_string": b"",
            "headers": [(b"idempotency-key", b"k")],
            "app": FastAPI(),
        }
        await middleware(scope, receive, send)
        assert (sent[0]["status"], len(incoming)) == (413, 98)

    async def test_other_response_messages_go_through(self) -> None:
        sent: list[Message] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
            await send({"type": "http.response.trailers", "headers": [], "more_trailers": False})

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "query_string": b"",
            "headers": [(b"idempotency-key", b"k")],
        }
        await IdempotencyMiddleware(app, cache=Cache(InMemoryBackend()))(scope, receive, send)
        assert [message["type"] for message in sent][-1] == "http.response.trailers"

    async def test_lifespan_and_websockets_go_through(self) -> None:
        seen: list[str] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            seen.append(scope["type"])

        middleware = IdempotencyMiddleware(app)
        for kind in ("lifespan", "websocket"):
            await middleware({"type": kind}, None, None)  # type: ignore[arg-type]
        assert seen == ["lifespan", "websocket"]


class TestValidation:
    @pytest.mark.parametrize(
        ("option", "value"), [("expire", 0), ("max_body_size", -1), ("max_response_size", -1)]
    )
    def test_options(self, option: str, value: int) -> None:
        options: dict[str, Any] = {option: value}
        with pytest.raises(ValueError, match="expire|sizes"):
            IdempotencyMiddleware(FastAPI(), **options)
