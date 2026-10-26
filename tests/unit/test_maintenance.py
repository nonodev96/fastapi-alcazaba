"""FastAPIMaintenance and MaintenanceMiddleware: a maintenance switch shared through the backend."""

import asyncio
import json
import logging
import time
from typing import Any

import fakeredis
import httpx2 as httpx
import pytest
from fastapi import FastAPI
from starlette.requests import HTTPConnection
from starlette.types import Message, Receive, Scope, Send

from fastapi_alcazaba import BackendUnavailable, Cache, FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.backends.tiered import TieredBackend
from fastapi_alcazaba.maintenance import (
    MAX_MESSAGE_LENGTH,
    FastAPIMaintenance,
    MaintenanceMiddleware,
    MaintenanceStatus,
)
from fastapi_alcazaba.middleware import _route_path
from fastapi_alcazaba.types import Backend

KEY = "app!maintenance:state"


@pytest.fixture(name="cache", params=["memory", "redis"])
def fixture_cache(request: pytest.FixtureRequest) -> Cache:
    backend: Backend = (
        InMemoryBackend()
        if request.param == "memory"
        else RedisBackend(fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer()))
    )
    return Cache(backend, prefix="app")


class Counting(InMemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.gets = 0
        self.failing = False
        self.gate: asyncio.Event | None = None

    async def get(self, key: str) -> bytes | None:
        self.gets += 1
        if self.failing:
            raise ConnectionError("down")
        value = await super().get(key)
        if self.gate is not None:
            await self.gate.wait()
        return value

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        if self.failing:
            raise ConnectionError("down")
        await super().set(key, value, expire)

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        if self.failing:
            raise ConnectionError("down")
        return await super().clear(namespace, key)


def app_with(maintenance: FastAPIMaintenance, **options: Any) -> FastAPI:
    app = FastAPI()

    @app.get("/{path:path}")
    async def anything(path: str) -> dict[str, str]:
        return {"path": path}

    app.add_middleware(MaintenanceMiddleware, maintenance=maintenance, **options)
    return app


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def ok(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})


async def call(middleware: MaintenanceMiddleware, scope: Scope) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


def websocket(path: str = "/ws") -> Scope:
    return {"type": "websocket", "path": path, "root_path": "", "headers": []}


class TestSwitch:
    async def test_enable_and_disable(self, cache: Cache) -> None:
        maintenance = FastAPIMaintenance(cache)
        assert await maintenance.status() == MaintenanceStatus(enabled=False)
        before = time.time()
        state = await maintenance.enable("Back at 10:30", retry_after=900)
        assert (state.enabled, state.message, state.retry_after) == (True, "Back at 10:30", 900)
        assert state.since is not None
        assert before <= state.since <= time.time()
        assert await maintenance.status() == state
        assert await maintenance.current() == state
        await maintenance.disable()
        assert not (await maintenance.status()).enabled
        assert not (await maintenance.current()).enabled

    async def test_shared_by_every_process(self, cache: Cache) -> None:
        here = FastAPIMaintenance(cache)
        elsewhere = FastAPIMaintenance(cache, refresh=0.1)
        assert not (await elsewhere.current()).enabled
        await here.enable()
        assert not (await elsewhere.current()).enabled  # its copy is still fresh
        await asyncio.sleep(0.12)
        assert (await elsewhere.current()).enabled
        await here.disable()
        await asyncio.sleep(0.12)
        assert not (await elsewhere.current()).enabled

    async def test_requests_dont_ask_the_backend_each(self) -> None:
        backend = Counting()
        maintenance = FastAPIMaintenance(Cache(backend), refresh=60)
        async with client(app_with(maintenance)) as http:
            for _ in range(10):
                assert (await http.get("/items")).status_code == 200
        assert backend.gets == 1

    async def test_in_the_shared_backend(self) -> None:
        remote = InMemoryBackend()
        await FastAPIMaintenance(Cache(TieredBackend(remote), prefix="app")).enable()
        assert await remote.get(KEY) is not None

    async def test_a_read_that_started_before_a_change_is_stale(self) -> None:
        backend = Counting()
        maintenance = FastAPIMaintenance(Cache(backend))
        gate = backend.gate = asyncio.Event()
        reading = asyncio.create_task(maintenance.status())
        await asyncio.sleep(0.01)  # it read "off", and waits
        backend.gate = None
        await maintenance.enable()
        gate.set()
        assert not (await reading).enabled  # what the backend had when it was read
        assert (await maintenance.current()).enabled  # the copy keeps the newer change

    async def test_through_fastapi_cache(self) -> None:
        FastAPICache.init(InMemoryBackend())
        maintenance = FastAPIMaintenance()
        await maintenance.enable()
        assert (await FastAPIMaintenance().status()).enabled

    async def test_a_new_fastapi_cache_drops_the_copy(self) -> None:
        """As each test's lifespan sets FastAPICache up again: the last app's state isn't served."""
        maintenance = FastAPIMaintenance(refresh=60)
        FastAPICache.init(InMemoryBackend())
        await maintenance.enable()
        assert (await maintenance.current()).enabled
        FastAPICache.reset()
        FastAPICache.init(InMemoryBackend())
        assert not (await maintenance.current()).enabled

    async def test_forget_drops_the_copy(self) -> None:
        backend = Counting()
        maintenance = FastAPIMaintenance(Cache(backend), refresh=60)
        await maintenance.current()
        await maintenance.current()
        assert backend.gets == 1
        maintenance.forget()
        await maintenance.current()
        assert backend.gets == 2


class TestMiddleware:
    async def test_refuses_with_503(self, cache: Cache) -> None:
        maintenance = FastAPIMaintenance(cache)
        await maintenance.enable("Back at 10:30", retry_after=900)
        async with client(app_with(maintenance)) as http:
            refused = await http.get("/items")
        assert refused.status_code == 503
        assert refused.json() == {"detail": "Back at 10:30"}
        assert refused.headers["retry-after"] == "900"
        assert refused.headers["cache-control"] == "no-store"

    async def test_without_message_nor_retry_after(self, cache: Cache) -> None:
        maintenance = FastAPIMaintenance(cache)
        await maintenance.enable()
        async with client(app_with(maintenance)) as http:
            refused = await http.get("/items")
        assert refused.status_code == 503
        assert refused.json() == {"detail": "Service Unavailable"}
        assert "retry-after" not in refused.headers

    async def test_serves_when_off(self, cache: Cache) -> None:
        async with client(app_with(FastAPIMaintenance(cache))) as http:
            assert (await http.get("/items")).json() == {"path": "items"}

    async def test_allowed_paths(self, cache: Cache) -> None:
        maintenance = FastAPIMaintenance(cache)
        await maintenance.enable()
        app = app_with(maintenance, allow_paths=("/health", "/admin/"))
        async with client(app) as http:
            for path in ("/health", "/health/live", "/admin", "/admin/users"):
                assert (await http.get(path)).status_code == 200, path
            for path in ("/healthz", "/administrator", "/"):
                assert (await http.get(path)).status_code == 503, path

    def test_paths_without_the_root_path(self) -> None:
        middleware = MaintenanceMiddleware(ok, maintenance=FastAPIMaintenance())
        scope = {"type": "http", "path": "/api/health", "root_path": "/api"}
        assert middleware.allowed_path(_route_path(scope))
        assert not middleware.allowed_path("/api/health")

    async def test_allowed_paths_dont_read_the_state(self) -> None:
        backend = Counting()
        maintenance = FastAPIMaintenance(Cache(backend))
        async with client(app_with(maintenance)) as http:
            assert (await http.get("/health")).status_code == 200
        assert backend.gets == 0

    @pytest.mark.parametrize("asynchronous", [False, True])
    async def test_allowed_requests(self, cache: Cache, asynchronous: bool) -> None:
        def from_the_team(connection: HTTPConnection) -> bool:
            return connection.headers.get("x-team") == "yes"

        async def from_the_team_async(connection: HTTPConnection) -> bool:
            return from_the_team(connection)

        maintenance = FastAPIMaintenance(cache)
        await maintenance.enable()
        allow = from_the_team_async if asynchronous else from_the_team
        async with client(app_with(maintenance, allow=allow)) as http:
            assert (await http.get("/items", headers={"x-team": "yes"})).status_code == 200
            assert (await http.get("/items")).status_code == 503

    async def test_websockets_are_closed_with_try_again_later(self, cache: Cache) -> None:
        maintenance = FastAPIMaintenance(cache)
        middleware = MaintenanceMiddleware(ok, maintenance=maintenance, allow_paths=("/ws/ok",))
        assert await call(middleware, websocket()) == [{"type": "websocket.accept"}]
        await maintenance.enable()
        closed = await call(middleware, websocket())
        assert closed == [{"type": "websocket.close", "code": 1013, "reason": ""}]
        assert await call(middleware, websocket("/ws/ok")) == [{"type": "websocket.accept"}]

    async def test_lifespan_goes_through(self, cache: Cache) -> None:
        maintenance = FastAPIMaintenance(cache)
        await maintenance.enable()
        seen: list[str] = []

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            seen.append(scope["type"])

        await call(MaintenanceMiddleware(app, maintenance=maintenance), {"type": "lifespan"})
        assert seen == ["lifespan"]


class TestBackendFailures:
    async def test_the_last_state_read_stays(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = Counting()
        cache = Cache(backend, circuit_breaker_failures=None)
        maintenance = FastAPIMaintenance(cache, refresh=0.05)
        await maintenance.enable("Back soon")
        backend.failing = True
        await asyncio.sleep(0.06)
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.maintenance"):
            assert (await maintenance.current()).message == "Back soon"
            gets = backend.gets
            assert (await maintenance.current()).enabled  # not asked again before `refresh`
            assert backend.gets == gets
        assert "not refreshed: the backend failed" in caplog.text
        with pytest.raises(BackendUnavailable):
            await maintenance.status()
        with pytest.raises(BackendUnavailable):
            await maintenance.disable()
        assert (await maintenance.current()).enabled

    async def test_never_read_serves(self) -> None:
        backend = Counting()
        backend.failing = True
        maintenance = FastAPIMaintenance(Cache(backend, circuit_breaker_failures=None))
        async with client(app_with(maintenance)) as http:
            assert (await http.get("/items")).status_code == 200
        with pytest.raises(BackendUnavailable):
            await maintenance.enable()
        assert not (await maintenance.current()).enabled

    async def test_an_open_circuit_is_only_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = Counting()
        backend.failing = True
        maintenance = FastAPIMaintenance(Cache(backend, circuit_breaker_failures=1), refresh=0.01)
        with caplog.at_level(logging.DEBUG, "fastapi_alcazaba.maintenance"):
            await maintenance.current()
            await asyncio.sleep(0.02)
            await maintenance.current()
        assert "circuit breaker is open" in caplog.text


class TestStoredState:
    async def test_planted_states_are_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = InMemoryBackend()
        cache = Cache(backend, prefix="app", signing_key="k" * 32)
        maintenance = FastAPIMaintenance(cache)
        await maintenance.enable("signed")
        assert (await maintenance.status()).message == "signed"
        planted = json.dumps({"message": "planted", "retry_after": None, "since": 1.0})
        await backend.set(KEY, planted.encode())
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.maintenance"):
            assert not (await maintenance.status()).enabled
        assert "didn't write" in caplog.text

    @pytest.mark.parametrize(
        "payload",
        [
            b"not json",
            b"[1, 2]",
            b'{"message": null, "retry_after": null}',
            b'{"message": 1, "retry_after": null, "since": 1}',
            b'{"message": null, "retry_after": "5", "since": 1}',
            b'{"message": null, "retry_after": true, "since": 1}',
            b'{"message": null, "retry_after": 0, "since": 1}',
            b'{"message": null, "retry_after": null, "since": "x"}',
            b'{"message": null, "retry_after": null, "since": true}',
            json.dumps({"message": "x" * 1001, "retry_after": None, "since": 1}).encode(),
        ],
    )
    async def test_malformed_states_are_ignored(
        self,
        payload: bytes,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        backend = InMemoryBackend()
        await backend.set(KEY, payload)
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.maintenance"):
            assert not (await FastAPIMaintenance(Cache(backend, prefix="app")).status()).enabled
        assert "malformed" in caplog.text

    async def test_states_written_by_others(self) -> None:
        backend = InMemoryBackend()
        payload = {"message": None, "retry_after": 30, "since": 5}
        await backend.set(KEY, json.dumps(payload).encode())
        state = await FastAPIMaintenance(Cache(backend, prefix="app")).status()
        assert state == MaintenanceStatus(True, None, 30, 5.0)


class TestValidation:
    async def test_enable(self, cache: Cache) -> None:
        maintenance = FastAPIMaintenance(cache)
        with pytest.raises(ValueError, match="message"):
            await maintenance.enable("x" * (MAX_MESSAGE_LENGTH + 1))
        with pytest.raises(ValueError, match="retry_after"):
            await maintenance.enable(retry_after=0)
        with pytest.raises(ValueError, match="retry_after"):
            await maintenance.enable(retry_after=True)
        assert not (await maintenance.status()).enabled

    def test_arguments(self) -> None:
        with pytest.raises(ValueError, match="refresh"):
            FastAPIMaintenance(refresh=0)
        with pytest.raises(ValueError, match="refresh"):
            FastAPIMaintenance(refresh=float("nan"))
        with pytest.raises(TypeError, match="not a single string"):
            MaintenanceMiddleware(ok, maintenance=FastAPIMaintenance(), allow_paths="/health")
        with pytest.raises(ValueError, match="start with /"):
            MaintenanceMiddleware(ok, maintenance=FastAPIMaintenance(), allow_paths=("health",))
