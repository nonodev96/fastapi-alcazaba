"""Liveness and readiness endpoints."""

import asyncio
import contextvars
import gc
import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_alcazaba import Cache, FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.observability import HealthCheck, health_router
from fastapi_alcazaba.observability.health import _call


@pytest.fixture(autouse=True)
def _no_global_cache() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    FastAPICache.reset()
    yield
    FastAPICache.reset()


def app_with(*args: Any, **kwargs: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(health_router(*args, **kwargs))
    return app


def client_for(*args: Any, **kwargs: Any) -> TestClient:
    return TestClient(app_with(*args, **kwargs))


async def passes() -> bool:
    return True


async def fails() -> None:
    raise ConnectionError("down")


class Down(InMemoryBackend):
    async def get(self, key: str) -> bytes | None:
        raise ConnectionError("down")


class TestLive:
    def test_the_process_answers_whatever_the_checks(self) -> None:
        response = client_for({"database": fails}).get("/health/live")
        assert (response.status_code, response.json()) == (200, {"status": "ok"})
        assert response.headers["cache-control"] == "no-store"


class TestReady:
    def test_every_check_passes(self) -> None:
        def sync() -> None:
            return None

        response = client_for({"database": passes, "queue": sync}).get("/health/ready")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["status"] == "ok"
        assert {name: check["status"] for name, check in body["checks"].items()} == {
            "database": "ok",
            "queue": "ok",
        }
        assert all(isinstance(check["duration_ms"], float) for check in body["checks"].values())

    def test_without_checks(self) -> None:
        response = client_for().get("/health/ready")
        assert (response.status_code, response.json()) == (200, {"status": "ok", "checks": {}})

    def test_a_critical_check_fails(self) -> None:
        response = client_for({"database": fails, "queue": passes}).get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert (body["status"], body["checks"]["database"]["status"]) == ("fail", "fail")
        assert body["checks"]["queue"]["status"] == "ok"

    def test_a_non_critical_check_fails(self) -> None:
        checks = {"database": passes, "search": HealthCheck(fails, critical=False)}
        response = client_for(checks).get("/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
        assert response.json()["checks"]["search"]["status"] == "fail"

    @pytest.mark.parametrize(("returned", "status"), [(False, "fail"), (None, "ok"), (0, "ok")])
    def test_only_false_fails(self, returned: object, status: str) -> None:
        async def check() -> object:
            return returned

        response = client_for({"check": check}).get("/health/ready")
        assert response.json()["checks"]["check"]["status"] == status

    def test_errors_are_logged_not_returned(self, caplog: pytest.LogCaptureFixture) -> None:
        """A public probe mustn't reveal hostnames or connection strings."""

        async def database() -> None:
            raise ConnectionError("postgresql://app:s3cret@db.internal:5432")

        with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.observability.health"):
            response = client_for({"database": database}).get("/health/ready")
        assert "s3cret" not in response.text
        assert "db.internal" not in response.text
        assert "'database' failed" in caplog.text
        assert "s3cret" in caplog.text  # the traceback, for whoever reads the logs

    def test_a_check_returning_false_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        async def check() -> bool:
            return False

        with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.observability.health"):
            client_for({"flag": check}).get("/health/ready")
        assert "'flag' returned False" in caplog.text

    def test_kinds_of_callables(self) -> None:
        class AsyncCallable:
            async def __call__(self) -> bool:
                return True

        async def coroutine() -> bool:
            return True

        def returns_a_coroutine() -> Any:
            return coroutine()

        checks = {"object": AsyncCallable(), "lambda": returns_a_coroutine}
        assert client_for(checks).get("/health/ready").json()["status"] == "ok"


class TestTimeouts:
    def test_a_slow_check_fails_at_its_timeout(self, caplog: pytest.LogCaptureFixture) -> None:
        async def slow() -> None:
            await asyncio.sleep(10)

        start = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.observability.health"):
            response = client_for({"slow": HealthCheck(slow, timeout=0.05)}).get("/health/ready")
        assert time.monotonic() - start < 2
        assert response.status_code == 503
        assert "'slow' didn't answer within 0.05s" in caplog.text

    def test_the_routers_timeout(self) -> None:
        async def slow() -> None:
            await asyncio.sleep(10)

        start = time.monotonic()
        response = client_for({"slow": slow}, timeout=0.05).get("/health/ready")
        assert time.monotonic() - start < 2
        assert response.json()["checks"]["slow"]["status"] == "fail"

    def test_a_hanging_sync_check_is_abandoned_at_its_timeout(self) -> None:
        """Its thread finishes on its own; the probe doesn't wait for it."""
        released = threading.Event()

        def hangs() -> None:
            released.wait(5)

        with TestClient(app_with({"hangs": hangs}, timeout=0.05)) as client:
            start = time.monotonic()
            response = client.get("/health/ready")
            elapsed = time.monotonic() - start
            released.set()
        assert response.status_code == 503
        assert elapsed < 2

    def test_a_hanging_sync_check_doesnt_pile_up_threads(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """While its thread hangs, later probes don't start another one."""
        released, calls = threading.Event(), []

        def hangs() -> None:
            calls.append(1)
            released.wait(5)

        with TestClient(app_with({"hangs": hangs}, timeout=0.05, reuse_for=0)) as client:
            client.get("/health/ready")
            with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.observability.health"):
                assert client.get("/health/ready").status_code == 503
            assert "still running since an earlier probe" in caplog.text
            assert calls == [1]
            released.set()
            deadline = time.monotonic() + 2
            while calls == [1] and time.monotonic() < deadline:
                client.get("/health/ready")  # runs again once the thread is done
                time.sleep(0.01)
        assert len(calls) >= 2

    def test_a_hanging_sync_check_doesnt_hold_up_the_shutdown(self) -> None:
        """asyncio.run (uvicorn) waits for the default executor's threads when it ends, not for
        the daemon thread of a check."""
        released, threads = threading.Event(), []

        def hangs() -> None:
            threads.append(threading.current_thread())
            released.wait(5)

        async def probe() -> int:
            async with asgi_client(app_with({"hangs": hangs}, timeout=0.05)) as client:
                return (await client.get("/health/ready")).status_code

        start = time.monotonic()
        assert asyncio.run(probe()) == 503
        assert time.monotonic() - start < 2
        released.set()  # it ends after the loop is closed: nothing to tell it, and no error
        threads[0].join(1)
        assert threads[0].daemon
        assert not threads[0].is_alive()

    async def test_a_check_that_fails_after_its_timeout(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Its failure, which nobody waits for any more, isn't reported again by asyncio."""
        released, threads = threading.Event(), []

        def fails_late() -> None:
            threads.append(threading.current_thread())
            released.wait(5)
            raise RuntimeError("too late")

        async with asgi_client(app_with({"late": fails_late}, timeout=0.05)) as client:
            assert (await client.get("/health/ready")).status_code == 503
            released.set()
            threads[0].join(1)
            await asyncio.sleep(0.01)  # the failure reaches the loop
        gc.collect()
        assert "never retrieved" not in caplog.text

    async def test_a_late_result_doesnt_let_a_third_thread_start(self) -> None:
        """The first thread is done, its result not handled yet, when a second probe starts
        another: a third probe still finds that one running."""
        threads: dict[str, threading.Thread] = {}
        first_done, released, runs = threading.Event(), threading.Event(), []

        def check() -> None:
            runs.append(1)
            if len(runs) == 1:
                first_done.wait(5)
            else:
                released.wait(5)

        first = asyncio.create_task(_call(check, "check", threads))
        await asyncio.sleep(0.01)
        first_done.set()
        threads["check"].join(1)  # blocks the loop: the result waits for it
        second = asyncio.create_task(_call(check, "check", threads))
        await asyncio.sleep(0.01)
        with pytest.raises(RuntimeError, match="still running since an earlier probe"):
            await _call(check, "check", threads)
        released.set()
        await asyncio.gather(first, second)
        assert len(runs) == 2

    def test_a_check_raising_its_own_timeout_is_a_failure(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def check() -> None:
            raise TimeoutError("the database's own timeout")

        with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.observability.health"):
            response = client_for({"database": check}).get("/health/ready")
        assert response.status_code == 503
        assert "'database' failed" in caplog.text


class TestThreads:
    def test_sync_checks_run_off_the_event_loop(self) -> None:
        threads: dict[str, threading.Thread] = {}

        async def on_the_loop() -> None:
            threads["loop"] = threading.current_thread()

        def blocking() -> None:
            threads["sync"] = threading.current_thread()

        client_for({"a": on_the_loop, "b": blocking}).get("/health/ready")
        assert threads["sync"] is not threads["loop"]

    def test_a_sync_check_that_raises_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        def database() -> None:
            raise ConnectionError("db.internal refused the connection")

        with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.observability.health"):
            response = client_for({"database": database}).get("/health/ready")
        assert response.status_code == 503
        assert "db.internal" not in response.text
        assert "'database' failed" in caplog.text
        assert "db.internal refused the connection" in caplog.text

    async def test_sync_checks_see_the_context(self) -> None:
        variable = contextvars.ContextVar("variable", default="unset")
        variable.set("set")

        def check() -> str:
            return variable.get()

        assert await _call(check, "check", {}) == "set"

    def test_the_same_callable_under_two_names(self) -> None:
        """Each name has its own thread: both run at once."""
        barrier = threading.Barrier(2, timeout=1)

        def ping() -> None:
            barrier.wait()

        response = client_for({"primary": ping, "replica": ping}).get("/health/ready")
        assert (response.status_code, response.json()["status"]) == (200, "ok")

    async def test_the_same_callable_in_two_routers(self) -> None:
        barrier = threading.Barrier(2, timeout=1)

        def ping() -> None:
            barrier.wait()

        app = FastAPI()
        app.include_router(health_router({"database": ping}, prefix="/a"))
        app.include_router(health_router({"database": ping}, prefix="/b"))
        async with asgi_client(app) as client:
            responses = await asyncio.gather(client.get("/a/ready"), client.get("/b/ready"))
        assert [response.status_code for response in responses] == [200, 200]


class TestCacheCheck:
    def test_the_global_cache(self) -> None:
        FastAPICache.init(InMemoryBackend())
        body = client_for().get("/health/ready").json()
        assert body == {"status": "ok", "checks": {"cache": body["checks"]["cache"]}}
        assert body["checks"]["cache"]["status"] == "ok"

    def test_the_cache_given_and_its_prefix(self) -> None:
        asked: list[str] = []

        class Recording(InMemoryBackend):
            async def get(self, key: str) -> bytes | None:
                asked.append(key)
                return await super().get(key)

        response = client_for(cache=Cache(Recording(), prefix="myapp")).get("/health/ready")
        assert response.json()["checks"]["cache"]["status"] == "ok"
        assert asked == ["myapp:fastapi_alcazaba:health"]

    def test_the_installed_cache(self) -> None:
        app = app_with()
        Cache(Down()).install(app)
        assert TestClient(app).get("/health/ready").json()["checks"]["cache"]["status"] == "fail"

    def test_a_failing_cache_only_degrades(self) -> None:
        """The cache fails open: its outage mustn't take every instance out of the balancer."""
        response = client_for({"database": passes}, cache=Cache(Down())).get("/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"

    async def test_an_open_circuit_breaker_doesnt_hide_that_the_backend_is_back(self) -> None:
        cache = Cache(InMemoryBackend(), circuit_breaker_failures=1)

        async def broken() -> None:
            raise ConnectionError("down")

        with pytest.raises(Exception, match="failed"):
            await cache.guard.run("get", broken)
        assert cache.guard.breaker is not None
        assert cache.guard.breaker.state == "open"
        response = client_for(cache=cache).get("/health/ready")
        assert response.json()["checks"]["cache"]["status"] == "ok"

    def test_left_out_without_a_cache(self) -> None:
        assert client_for().get("/health/ready").json()["checks"] == {}

    def test_left_out_when_asked(self) -> None:
        FastAPICache.init(InMemoryBackend())
        response = client_for(include_cache=False).get("/health/ready")
        assert response.json()["checks"] == {}

    def test_the_name_is_taken(self) -> None:
        with pytest.raises(ValueError, match="built-in cache check"):
            health_router({"cache": passes})
        assert health_router({"cache": passes}, include_cache=False)


class Gate:
    """A check that waits until it's released, counting its runs."""

    def __init__(self) -> None:
        self.runs = 0
        self.released = asyncio.Event()

    async def __call__(self) -> None:
        self.runs += 1
        await self.released.wait()


def asgi_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestSharedRuns:
    async def test_concurrent_probes_share_one_run(self) -> None:
        gate = Gate()
        async with asgi_client(app_with({"gate": gate}, reuse_for=0)) as client:
            probes = [asyncio.create_task(client.get("/health/ready")) for _ in range(5)]
            await asyncio.sleep(0.05)
            gate.released.set()
            responses = await asyncio.gather(*probes)
        assert [response.status_code for response in responses] == [200] * 5
        assert gate.runs == 1

    async def test_a_probe_that_goes_away_doesnt_cancel_the_others(self) -> None:
        gate = Gate()
        async with asgi_client(app_with({"gate": gate})) as client:
            first = asyncio.create_task(client.get("/health/ready"))
            second = asyncio.create_task(client.get("/health/ready"))
            await asyncio.sleep(0.05)
            first.cancel()
            await asyncio.sleep(0)
            gate.released.set()
            response = await second
        assert first.cancelled()
        assert (response.status_code, gate.runs) == (200, 1)

    def test_results_are_reused_for_a_while(self) -> None:
        runs: list[float] = []

        async def check() -> None:
            runs.append(time.monotonic())

        client = client_for({"check": check}, reuse_for=60)
        assert client.get("/health/ready").json() == client.get("/health/ready").json()
        assert len(runs) == 1

    def test_then_the_checks_run_again(self) -> None:
        runs: list[float] = []

        async def check() -> None:
            runs.append(time.monotonic())

        client = client_for({"check": check}, reuse_for=0.05)
        client.get("/health/ready")
        time.sleep(0.1)
        client.get("/health/ready")
        assert len(runs) == 2

    def test_no_reuse(self) -> None:
        runs: list[float] = []

        async def check() -> None:
            runs.append(time.monotonic())

        with TestClient(app_with({"check": check}, reuse_for=0)) as client:
            for _ in range(3):
                client.get("/health/ready")
        assert len(runs) == 3


class TestRouter:
    def test_hidden_from_the_schema_by_default(self) -> None:
        assert not any(path.startswith("/health") for path in app_with().openapi()["paths"])

    def test_in_the_schema_when_asked(self) -> None:
        paths = app_with(include_in_schema=True, tags=["ops"]).openapi()["paths"]
        assert set(paths) == {"/health/live", "/health/ready"}
        assert paths["/health/ready"]["get"]["tags"] == ["ops"]
        assert "503" in paths["/health/ready"]["get"]["responses"]

    @pytest.mark.parametrize(
        ("prefix", "path"), [("/_internal", "/_internal/ready"), ("", "/ready")]
    )
    def test_prefix(self, prefix: str, path: str) -> None:
        assert client_for(prefix=prefix).get(path).status_code == 200

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"timeout": 0}, "timeout"),
            ({"reuse_for": -1}, "reuse_for"),
            ({"prefix": "health"}, "prefix"),
            ({"prefix": "/health/"}, "prefix"),
        ],
    )
    def test_validation(self, kwargs: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            health_router(**kwargs)

    def test_check_timeouts_are_positive(self) -> None:
        with pytest.raises(ValueError, match="timeout"):
            HealthCheck(passes, timeout=0)
