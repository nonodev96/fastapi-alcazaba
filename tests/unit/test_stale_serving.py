"""Serving stale entries: stale-while-revalidate (served at once, refreshed after the response,
once) and stale-if-error (served when computing fails or takes too long), on a fake clock."""

import asyncio
import logging
import time
from typing import Any

import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.response_cache import cache
from tests.unit.test_metrics import points


class Clock:
    """The time InMemoryBackend's entries expire by."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return time.time()


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr("fastapi_alcazaba.backends.inmemory.time", clock)
    return clock


@pytest.fixture(name="reader")
def fixture_reader() -> InMemoryMetricReader:
    reader = InMemoryMetricReader()
    FastAPICache.init(InMemoryBackend(), meter_provider=MeterProvider(metric_readers=[reader]))
    return reader


class Source:
    """What the endpoint reads: a counter, which can fail or hang."""

    def __init__(self) -> None:
        self.calls = 0
        self.failing = False
        self.delay = 0.0

    async def read(self) -> dict[str, int]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failing:
            raise ConnectionError("the database is down")
        return {"n": self.calls}


def app_with(source: Source, **options: Any) -> TestClient:
    app = FastAPI()

    @app.get("/data")
    @cache(expire=10, **options)
    async def data() -> dict[str, int]:
        return await source.read()

    return TestClient(app, raise_server_exceptions=False)


class TestStaleWhileRevalidate:
    def test_served_at_once_and_refreshed_after(self, clock: Clock, reader: Any) -> None:
        source = Source()
        client = app_with(source, stale_while_revalidate=30)
        first = client.get("/data")
        assert first.json() == {"n": 1}
        assert first.headers["cache-control"] == "max-age=10, stale-while-revalidate=30"
        clock.now += 15
        stale = client.get("/data")
        assert stale.json() == {"n": 1}  # at once, the old one
        assert stale.headers["cache-status"] == (
            "Alcazaba; hit; ttl=-5; detail=stale-while-revalidate"
        )
        assert stale.headers["cache-control"] == "stale-while-revalidate=30"
        assert source.calls == 2  # refreshed after the response
        fresh = client.get("/data")
        assert fresh.json() == {"n": 2}
        assert fresh.headers["cache-status"].startswith("Alcazaba; hit; ttl=10")
        results = {
            a["fastapi_alcazaba.result"]: v for a, v in points(reader, "fastapi_alcazaba.lookups")
        }
        assert results["stale"] == 1

    def test_past_the_window_it_is_a_miss(self, clock: Clock, reader: Any) -> None:
        source = Source()
        client = app_with(source, stale_while_revalidate=30)
        client.get("/data")
        clock.now += 10 + 31
        response = client.get("/data")
        assert response.json() == {"n": 2}
        assert "fwd=miss" in response.headers["cache-status"]
        del reader

    def test_a_refresh_that_fails_keeps_the_stale_one(
        self,
        clock: Clock,
        reader: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        source = Source()
        client = app_with(source, stale_while_revalidate=30)
        client.get("/data")
        clock.now += 12
        source.failing = True
        assert client.get("/data").json() == {"n": 1}
        assert "Refreshing" in caplog.text
        assert client.get("/data").json() == {"n": 1}  # tried again, still stale
        assert source.calls == 3
        source.failing = False
        client.get("/data")
        assert client.get("/data").json() == {"n": 4}
        del reader

    def test_an_endpoint_that_takes_background_tasks_itself(
        self,
        clock: Clock,
        reader: Any,
    ) -> None:
        app = FastAPI()
        source, done = Source(), list[str]()

        @app.get("/data")
        @cache(expire=10, stale_while_revalidate=30)
        async def data(background_tasks: BackgroundTasks) -> dict[str, int]:
            background_tasks.add_task(done.append, "its own")
            return await source.read()

        client = TestClient(app)
        client.get("/data")
        clock.now += 11
        assert client.get("/data").json() == {"n": 1}
        assert source.calls == 2
        assert done == ["its own", "its own"]  # the refresh ran it too
        del reader

    def test_signed_entries(self, clock: Clock) -> None:
        FastAPICache.init(InMemoryBackend(), signing_key="k" * 32)
        source = Source()
        client = app_with(source, stale_while_revalidate=30)
        client.get("/data")
        clock.now += 11
        assert client.get("/data").json() == {"n": 1}
        assert client.get("/data").json() == {"n": 2}


class TestOutsideRoutes:
    async def test_one_refresh_per_process(self, clock: Clock, reader: Any) -> None:
        source = Source()
        source.delay = 0.05

        @cache(expire=10, stale_while_revalidate=30)
        async def report() -> dict[str, int]:
            return await source.read()

        assert await report() == {"n": 1}
        clock.now += 11
        results = await asyncio.gather(*(report() for _ in range(10)))
        assert results == [{"n": 1}] * 10
        await asyncio.sleep(0.1)  # the refresh, an asyncio task
        assert source.calls == 2
        assert await report() == {"n": 2}
        del reader

    async def test_another_process_refreshing_it(self, clock: Clock, reader: Any) -> None:
        source = Source()

        @cache(expire=10, stale_while_revalidate=30, namespace="shared")
        async def report() -> dict[str, int]:
            return await source.read()

        await report()
        clock.now += 11
        backend = FastAPICache.get_backend()
        assert isinstance(backend, InMemoryBackend)
        key = next(k for k in backend._store if "shared" in k)  # pylint: disable=protected-access
        await backend.acquire_lock(f"{key}:revalidate", "another process", 30)
        assert await report() == {"n": 1}
        await asyncio.sleep(0.01)
        assert source.calls == 1  # the other process refreshes it
        del reader


class TestStaleIfError:
    def test_served_when_computing_fails(self, clock: Clock, reader: Any) -> None:
        source = Source()
        client = app_with(source, stale_if_error=60)
        client.get("/data")
        clock.now += 20
        source.failing = True
        response = client.get("/data")
        assert (response.status_code, response.json()) == (200, {"n": 1})
        assert response.headers["cache-status"] == ("Alcazaba; hit; ttl=-10; detail=stale-if-error")
        results = {
            a["fastapi_alcazaba.result"]: v for a, v in points(reader, "fastapi_alcazaba.lookups")
        }
        assert results["stale_if_error"] == 1
        source.failing = False
        fresh = client.get("/data")
        assert fresh.json() == {"n": 3}
        assert "fwd=stale; stored" in fresh.headers["cache-status"]

    def test_served_when_computing_takes_too_long(self, clock: Clock, reader: Any) -> None:
        source = Source()
        client = app_with(source, stale_if_error=60, refresh_timeout=0.05)
        client.get("/data")
        clock.now += 20
        source.delay = 1
        started = time.perf_counter()
        assert client.get("/data").json() == {"n": 1}
        assert time.perf_counter() - started < 0.5
        del reader

    def test_past_the_window_the_error_goes_through(self, clock: Clock, reader: Any) -> None:
        source = Source()
        client = app_with(source, stale_if_error=60)
        client.get("/data")
        clock.now += 10 + 61
        source.failing = True
        assert client.get("/data").status_code == 500
        del reader

    def test_without_stale_if_error_the_error_goes_through(self, clock: Clock, reader: Any) -> None:
        source = Source()
        client = app_with(source)
        client.get("/data")
        clock.now += 11
        source.failing = True
        assert client.get("/data").status_code == 500
        del reader

    def test_both_windows(
        self,
        clock: Clock,
        reader: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        del reader
        caplog.set_level(logging.WARNING)
        source = Source()
        client = app_with(source, stale_while_revalidate=5, stale_if_error=60)
        client.get("/data")
        clock.now += 10 + 20  # past stale-while-revalidate, within stale-if-error
        source.failing = True
        response = client.get("/data")
        assert response.json() == {"n": 1}
        assert "stale-if-error" in response.headers["cache-status"]


class TestConfiguration:
    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"stale_while_revalidate": 0}, "stale_while_revalidate"),
            ({"stale_if_error": -1}, "stale_if_error"),
            ({"refresh_timeout": 1}, "goes with stale_if_error"),
            ({"stale_if_error": 10, "refresh_timeout": 0}, "positive"),
        ],
    )
    def test_invalid(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            cache(expire=10, **options)

    async def test_without_expiry_nothing_is_stale(self, reader: Any) -> None:
        source = Source()

        @cache(stale_while_revalidate=30)
        async def forever() -> dict[str, int]:
            return await source.read()

        assert await forever() == await forever() == {"n": 1}
        del reader


class WithoutLocks(InMemoryBackend):
    supports_locks = False


class FailingSets(InMemoryBackend):
    failing = False

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        if self.failing:
            raise ConnectionError("down")
        await super().set(key, value, expire)


class TestEdges:
    def test_a_backend_without_locks_refreshes_per_process(self, clock: Clock) -> None:
        FastAPICache.init(WithoutLocks())
        source = Source()
        client = app_with(source, stale_while_revalidate=30)
        client.get("/data")
        clock.now += 11
        assert client.get("/data").json() == {"n": 1}
        assert client.get("/data").json() == {"n": 2}

    def test_a_refresh_that_cant_store(self, clock: Clock) -> None:
        backend = FailingSets()
        FastAPICache.init(backend)
        source = Source()
        client = app_with(source, stale_while_revalidate=30)
        client.get("/data")
        clock.now += 11
        backend.failing = True
        assert client.get("/data").json() == {"n": 1}
        assert source.calls == 2  # computed, not stored: still stale
        assert client.get("/data").json() == {"n": 1}

    def test_a_stale_entry_that_cant_be_read(self, clock: Clock) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend)
        source = Source()
        client = app_with(source, stale_while_revalidate=30, stale_if_error=30)
        client.get("/data")
        clock.now += 11
        for key in list(backend._store):  # pylint: disable=protected-access
            backend._store[key].data = b"\x00not an entry"  # pylint: disable=protected-access
        response = client.get("/data")  # neither served stale: computed
        assert response.json() == {"n": 2}
        clock.now += 11
        for key in list(backend._store):  # pylint: disable=protected-access
            backend._store[key].data = b"\x00not an entry"  # pylint: disable=protected-access
        source.failing = True
        assert client.get("/data").status_code == 500  # nothing usable to fall back on
