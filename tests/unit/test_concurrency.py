"""FastAPIConcurrencyLimiter: simultaneous requests across processes, on lock slots."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import fakeredis
import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection, Request
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba import BackendUnavailable, Cache, FastAPICache
from fastapi_alcazaba.backends import Backend
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.rate_limit import ConcurrencyLimitExceeded, FastAPIConcurrencyLimiter
from fastapi_alcazaba.rate_limit.concurrency import MAX_LIMIT


@pytest.fixture(name="cache", params=["memory", "redis"])
def fixture_cache(request: pytest.FixtureRequest) -> Cache:
    backend: Backend = (
        InMemoryBackend()
        if request.param == "memory"
        else RedisBackend(fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer()))
    )
    return Cache(backend, prefix="app")


class Flaky(InMemoryBackend):
    failing = False

    async def acquire_lock(self, key: str, token: str, ttl: float) -> bool:
        self._check()
        return await super().acquire_lock(key, token, ttl)

    async def extend_lock(self, key: str, token: str, ttl: float) -> bool:
        self._check()
        return await super().extend_lock(key, token, ttl)

    async def release_lock(self, key: str, token: str) -> None:
        self._check()
        await super().release_lock(key, token)

    def _check(self) -> None:
        if self.failing:
            raise ConnectionError("down")


class Gate:
    """An endpoint that holds its slot until the test opens the gate."""

    def __init__(self) -> None:
        self.entered = 0
        self.opened = asyncio.Event()

    async def __call__(self) -> dict[str, bool]:
        self.entered += 1
        await self.opened.wait()
        return {"ok": True}


def gated_app(*limiters: FastAPIConcurrencyLimiter, path: str = "/work") -> tuple[FastAPI, Gate]:
    app, gate = FastAPI(), Gate()
    app.get(path, dependencies=[Depends(limiter) for limiter in limiters])(gate)
    return app, gate


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


async def is_full(limiter: FastAPIConcurrencyLimiter, key: str = "") -> bool:
    try:
        async with limiter.slot(key):
            return False
    except ConcurrencyLimitExceeded:
        return True


def by_header(connection: HTTPConnection) -> str:
    return connection.headers.get("x-user", "")


class TestLimit:
    async def test_full_slots_answer_503_with_retry_after(self, cache: Cache) -> None:
        app, gate = gated_app(FastAPIConcurrencyLimiter(2, cache=cache))
        async with client(app) as http:
            running = [asyncio.create_task(http.get("/work")) for _ in range(2)]
            await until(lambda: gate.entered == 2)
            refused = await http.get("/work")
            assert refused.status_code == 503
            assert refused.json() == {"detail": "Service Unavailable"}
            assert refused.headers["retry-after"] == "1"
            gate.opened.set()
            assert [(await task).status_code for task in running] == [200, 200]
            # Released: the next ones run.
            assert [(await http.get("/work")).status_code for _ in range(3)] == [200] * 3

    async def test_per_client_answers_429(self, cache: Cache) -> None:
        limiter = FastAPIConcurrencyLimiter(1, identifier=by_header, cache=cache, retry_after=7)
        app, gate = gated_app(limiter)
        async with client(app) as http:
            alice = asyncio.create_task(http.get("/work", headers={"x-user": "alice"}))
            await until(lambda: gate.entered == 1)
            refused = await http.get("/work", headers={"x-user": "alice"})
            assert refused.status_code == 429
            assert refused.json() == {"detail": "Too Many Requests"}
            assert refused.headers["retry-after"] == "7"
            bob = asyncio.create_task(http.get("/work", headers={"x-user": "bob"}))
            await until(lambda: gate.entered == 2)  # a slot of his own
            gate.opened.set()
            assert (await alice).status_code == (await bob).status_code == 200

    async def test_async_identifiers(self, cache: Cache) -> None:
        async def identify(connection: HTTPConnection) -> str:
            return by_header(connection)

        app, gate = gated_app(FastAPIConcurrencyLimiter(1, identifier=identify, cache=cache))
        async with client(app) as http:
            alice = asyncio.create_task(http.get("/work", headers={"x-user": "alice"}))
            await until(lambda: gate.entered == 1)
            assert (await http.get("/work", headers={"x-user": "alice"})).status_code == 429
            gate.opened.set()
            assert (await alice).status_code == 200

    async def test_waiting_for_a_slot(self, cache: Cache) -> None:
        app, gate = gated_app(FastAPIConcurrencyLimiter(1, wait=5, cache=cache))
        async with client(app) as http:
            first = asyncio.create_task(http.get("/work"))
            await until(lambda: gate.entered == 1)
            second = asyncio.create_task(http.get("/work"))
            await asyncio.sleep(0.1)
            assert gate.entered == 1  # waiting
            gate.opened.set()
            assert (await first).status_code == (await second).status_code == 200

    async def test_giving_up_after_waiting(self, cache: Cache) -> None:
        app, gate = gated_app(FastAPIConcurrencyLimiter(1, wait=0.2, cache=cache))
        loop = asyncio.get_running_loop()
        async with client(app) as http:
            first = asyncio.create_task(http.get("/work"))
            await until(lambda: gate.entered == 1)
            started = loop.time()
            assert (await http.get("/work")).status_code == 503
            assert 0.2 <= loop.time() - started < 1
            gate.opened.set()
            await first

    async def test_routes_have_their_own_slots(self, cache: Cache) -> None:
        limiter = FastAPIConcurrencyLimiter(1, cache=cache)
        app, gate = gated_app(limiter, path="/a/{item}")
        app.get("/b", dependencies=[Depends(limiter)])(gate)
        async with client(app) as http:
            held = asyncio.create_task(http.get("/a/1"))
            await until(lambda: gate.entered == 1)
            assert (await http.get("/a/2")).status_code == 503  # same route, other path
            other = asyncio.create_task(http.get("/b"))
            await until(lambda: gate.entered == 2)
            gate.opened.set()
            assert (await held).status_code == (await other).status_code == 200

    async def test_a_name_shares_the_slots(self, cache: Cache) -> None:
        app, gate = gated_app(FastAPIConcurrencyLimiter(1, name="ai", cache=cache), path="/a")
        app.get("/b", dependencies=[Depends(FastAPIConcurrencyLimiter(1, name="ai", cache=cache))])(
            gate
        )
        async with client(app) as http:
            held = asyncio.create_task(http.get("/a"))
            await until(lambda: gate.entered == 1)
            assert (await http.get("/b")).status_code == 503
            gate.opened.set()
            await held

    async def test_released_when_the_endpoint_fails(self, cache: Cache) -> None:
        limiter = FastAPIConcurrencyLimiter(1, name="ocr", cache=cache)
        app = FastAPI()

        @app.get("/fail", dependencies=[Depends(limiter)])
        async def fail() -> None:
            raise HTTPException(418)

        async with client(app) as http:
            assert (await http.get("/fail")).status_code == 418
        assert not await is_full(limiter)
        assert limiter._held == {}

    async def test_the_installed_cache(self) -> None:
        installed = Cache(InMemoryBackend())
        app, gate = gated_app(FastAPIConcurrencyLimiter(1))
        installed.install(app)
        async with client(app) as http:
            held = asyncio.create_task(http.get("/work"))
            await until(lambda: gate.entered == 1)
            assert (await http.get("/work")).status_code == 503
            gate.opened.set()
            await held

    async def test_identities_are_hashed(self) -> None:
        backend = InMemoryBackend()
        limiter = FastAPIConcurrencyLimiter(
            1, identifier=lambda _: "user@example.com", cache=Cache(backend)
        )
        app, gate = gated_app(limiter)
        async with client(app) as http:
            held = asyncio.create_task(http.get("/work"))
            await until(lambda: gate.entered == 1)
            assert len(backend._locks) == 1
            key = next(iter(backend._locks))
            assert "example" not in key
            assert all("example" not in index for index in limiter._held)
            gate.opened.set()
            await held


class TestStreaming:
    async def test_held_until_the_body_is_sent(self, cache: Cache) -> None:
        limiter = FastAPIConcurrencyLimiter(1, name="pdf", cache=cache)
        seen: list[bool] = []
        app = FastAPI()

        @app.get("/pdf", dependencies=[Depends(limiter)])
        async def pdf() -> StreamingResponse:
            async def body() -> AsyncIterator[bytes]:
                yield b"a"
                seen.append(await is_full(limiter))
                yield b"b"

            return StreamingResponse(body())

        async with client(app) as http:
            assert (await http.get("/pdf")).content == b"ab"
        assert seen == [True]
        assert not await is_full(limiter)

    async def test_function_scope_releases_it_before_the_body(self, cache: Cache) -> None:
        limiter = FastAPIConcurrencyLimiter(1, name="pdf", cache=cache)
        seen: list[bool] = []
        app = FastAPI()

        @app.get("/pdf", dependencies=[Depends(limiter, scope="function")])
        async def pdf() -> StreamingResponse:
            async def body() -> AsyncIterator[bytes]:
                seen.append(await is_full(limiter))
                yield b"a"

            return StreamingResponse(body())

        async with client(app) as http:
            assert (await http.get("/pdf")).content == b"a"
        assert seen == [False]


class TestWebSockets:
    @pytest.mark.parametrize(("identifier", "code"), [(None, 1013), (by_header, 1008)])
    def test_held_while_connected(
        self,
        identifier: Callable[[HTTPConnection], str] | None,
        code: int,
    ) -> None:
        limiter = FastAPIConcurrencyLimiter(
            1, identifier=identifier, cache=Cache(InMemoryBackend())
        )
        app = FastAPI()

        @app.websocket("/ws", dependencies=[Depends(limiter)])
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_text(await websocket.receive_text())

        test_client = TestClient(app)
        with test_client.websocket_connect("/ws") as connection:
            with pytest.raises(WebSocketDisconnect) as refused:
                with test_client.websocket_connect("/ws"):
                    pass  # pragma: no cover
            assert refused.value.code == code
            connection.send_text("hi")
            assert connection.receive_text() == "hi"
        with test_client.websocket_connect("/ws") as connection:  # released
            connection.send_text("again")
            assert connection.receive_text() == "again"

    def test_a_failing_backend_closes_with_try_again_later(self) -> None:
        backend = Flaky()
        backend.failing = True
        limiter = FastAPIConcurrencyLimiter(1, cache=Cache(backend), fail_open=False)
        app = FastAPI()

        @app.websocket("/ws", dependencies=[Depends(limiter)])
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()  # pragma: no cover  # refused before

        with (
            pytest.raises(WebSocketDisconnect) as refused,
            TestClient(app).websocket_connect("/ws"),
        ):
            pass
        assert refused.value.code == 1013


class TestSlot:
    async def test_outside_requests(self, cache: Cache) -> None:
        limiter = FastAPIConcurrencyLimiter(2, name="reports", cache=cache)
        async with limiter.slot(), limiter.slot():
            with pytest.raises(ConcurrencyLimitExceeded) as refused:
                async with limiter.slot():
                    pass  # pragma: no cover
            assert refused.value.status_code == 503
            assert refused.value.retry_after == 1
            assert refused.value.headers == {"Retry-After": "1"}
        async with limiter.slot():
            pass

    async def test_keys_have_their_own_slots(self, cache: Cache) -> None:
        limiter = FastAPIConcurrencyLimiter(1, name="reports", cache=cache)
        async with limiter.slot("tenant-1"):
            assert await is_full(limiter, "tenant-1")
            assert not await is_full(limiter, "tenant-2")

    async def test_per_client_limiters_refuse_with_429(self, cache: Cache) -> None:
        limiter = FastAPIConcurrencyLimiter(1, name="r", identifier=by_header, cache=cache)
        async with limiter.slot():
            with pytest.raises(ConcurrencyLimitExceeded) as refused:
                async with limiter.slot():
                    pass  # pragma: no cover
        assert refused.value.status_code == 429

    async def test_with_the_request(self, cache: Cache) -> None:
        """Around the expensive part only, with the route and the identifier of the request."""
        limiter = FastAPIConcurrencyLimiter(1, identifier=by_header, cache=cache)
        app = FastAPI()
        inside: list[bool] = []

        @app.get("/work")
        async def work(request: Request) -> None:
            async with limiter.slot(connection=request):
                inside.append(await full_for(request))
            inside.append(await full_for(request))

        async def full_for(request: Request) -> bool:
            try:
                async with limiter.slot(connection=request):
                    return False
            except ConcurrencyLimitExceeded:
                return True

        async with client(app) as http:
            assert (await http.get("/work", headers={"x-user": "alice"})).status_code == 200
        assert inside == [True, False]

    async def test_needs_a_name_outside_requests(self, cache: Cache) -> None:
        with pytest.raises(ValueError, match="needs a limiter with a name"):
            async with FastAPIConcurrencyLimiter(1, cache=cache).slot():
                pass  # pragma: no cover

    async def test_through_fastapi_cache(self) -> None:
        FastAPICache.init(InMemoryBackend())
        limiter = FastAPIConcurrencyLimiter(1, name="reports")
        async with limiter.slot():
            assert await is_full(limiter)


class TestExpiry:
    async def test_renewed_while_held(self, cache: Cache) -> None:
        limiter = FastAPIConcurrencyLimiter(1, name="long", ttl=0.15, cache=cache)
        other_process = FastAPIConcurrencyLimiter(1, name="long", cache=cache)
        async with limiter.slot():
            await asyncio.sleep(0.4)
            assert await is_full(other_process)

    async def test_a_dead_holder_loses_its_slots(self, cache: Cache) -> None:
        dead = FastAPIConcurrencyLimiter(1, name="long", ttl=0.15, cache=cache)
        slot = await dead._take(None, "")
        assert slot is not None
        assert slot.lock is not None
        assert slot.lock._renewal is not None
        slot.lock._renewal.cancel()  # the process died: nobody renews it
        other_process = FastAPIConcurrencyLimiter(1, name="long", cache=cache)
        assert await is_full(other_process)
        await asyncio.sleep(0.2)
        assert not await is_full(other_process)


class TestBackendFailures:
    async def test_fail_open_counts_in_the_process(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = Flaky()
        backend.failing = True
        limiter = FastAPIConcurrencyLimiter(1, cache=Cache(backend, circuit_breaker_failures=None))
        app, gate = gated_app(limiter)
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.rate_limit.concurrency"):
            async with client(app) as http:
                held = asyncio.create_task(http.get("/work"))
                await until(lambda: gate.entered == 1)
                assert (await http.get("/work")).status_code == 503
                gate.opened.set()
                assert (await held).status_code == 200
        assert "counted in this process: the backend failed" in caplog.text
        assert limiter._local == {}

    async def test_several_slots_in_the_process(self) -> None:
        backend = Flaky()
        backend.failing = True
        limiter = FastAPIConcurrencyLimiter(
            2, name="r", cache=Cache(backend, circuit_breaker_failures=None)
        )
        async with limiter.slot():
            async with limiter.slot():
                assert list(limiter._local.values()) == [2]
                assert await is_full(limiter)
            assert list(limiter._local.values()) == [1]
        assert limiter._local == {}

    async def test_slots_held_in_the_backend_count_too(self) -> None:
        backend = Flaky()
        limiter = FastAPIConcurrencyLimiter(
            2, name="r", cache=Cache(backend, circuit_breaker_failures=None)
        )
        async with limiter.slot():
            backend.failing = True
            async with limiter.slot():  # the second one, in the process
                assert limiter._local
                assert await is_full(limiter)
        assert limiter._held == {}
        assert limiter._local == {}

    async def test_an_open_circuit_is_only_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = Flaky()
        backend.failing = True
        limiter = FastAPIConcurrencyLimiter(
            1, name="r", cache=Cache(backend, circuit_breaker_failures=1)
        )
        with caplog.at_level(logging.DEBUG, "fastapi_alcazaba.rate_limit.concurrency"):
            async with limiter.slot():
                pass
            async with limiter.slot():
                pass
        assert "circuit breaker is open" in caplog.text

    async def test_fail_closed_answers_503(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = Flaky()
        backend.failing = True
        limiter = FastAPIConcurrencyLimiter(1, cache=Cache(backend), fail_open=False)
        app, _ = gated_app(limiter)
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.rate_limit.concurrency"):
            async with client(app) as http:
                refused = await http.get("/work")
        assert refused.status_code == 503
        assert refused.headers["retry-after"] == "1"
        assert "refused: the backend failed" in caplog.text

    async def test_fail_closed_slots_raise(self) -> None:
        backend = Flaky()
        backend.failing = True
        limiter = FastAPIConcurrencyLimiter(1, name="r", cache=Cache(backend), fail_open=False)
        with pytest.raises(BackendUnavailable):
            async with limiter.slot():
                pass  # pragma: no cover


class TestValidation:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"limit": 0}, "limit"),
            ({"limit": MAX_LIMIT + 1}, "limit"),
            ({"wait": -1}, "wait"),
            ({"wait": float("inf")}, "wait"),
            ({"ttl": 0}, "ttl"),
            ({"ttl": float("nan")}, "ttl"),
            ({"retry_after": 0}, "retry_after"),
        ],
    )
    def test_arguments(self, kwargs: dict[str, Any], message: str) -> None:
        arguments: dict[str, Any] = {"limit": 1, **kwargs}
        with pytest.raises(ValueError, match=message):
            FastAPIConcurrencyLimiter(**arguments)

    async def test_backends_without_locks(self) -> None:
        class WithoutLocks(InMemoryBackend):
            supports_locks = False

        with pytest.raises(NotImplementedError, match="WithoutLocks can't hold locks"):
            FastAPIConcurrencyLimiter(1, cache=Cache(WithoutLocks()))
        app, _ = gated_app(FastAPIConcurrencyLimiter(1))
        Cache(WithoutLocks()).install(app)
        async with client(app) as http:
            with pytest.raises(NotImplementedError, match="can't hold locks"):
                await http.get("/work")
