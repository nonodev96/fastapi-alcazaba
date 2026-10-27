"""The Cache-Status header (RFC 9211) of cached endpoints: a hit and its freshness, or why the
request reached the endpoint and whether its response was stored."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import random

import httpx2 as httpx
import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.response_cache import cache
from tests.cache_status import cache_status


def make_app(delay: float = 0.0) -> tuple[FastAPI, list[int]]:
    app = FastAPI()
    calls: list[int] = []

    @app.get("/value")
    @cache(expire=100)
    async def value() -> dict[str, int]:
        calls.append(1)
        await asyncio.sleep(delay)
        return {"calls": len(calls)}

    @app.get("/inner")
    @cache(expire=100)
    async def inner(response: Response) -> dict[str, int]:
        # A cache of the endpoint's own, closer to the origin than Alcazaba's.
        response.headers["Cache-Status"] = "Database; hit"
        return {"value": 1}

    return app, calls


def test_a_miss_then_a_hit_with_its_freshness() -> None:
    FastAPICache.init(InMemoryBackend())
    app, _ = make_app()
    with TestClient(app) as client:
        miss, hit = client.get("/value"), client.get("/value")
    assert miss.headers["Cache-Status"] == "Alcazaba; fwd=miss; stored"
    assert hit.headers["Cache-Status"] in ("Alcazaba; hit; ttl=100", "Alcazaba; hit; ttl=99")


def test_an_early_refresh_is_a_stale_entry_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = InMemoryBackend()
    FastAPICache.init(backend, early_refresh=0.5)
    app, calls = make_app()
    with TestClient(app) as client:
        client.get("/value")
        for entry in backend._store.values():
            entry.expires_at -= 90  # type: ignore[operator]  # 10 s left of 100: refresh early
        monkeypatch.setattr(random, "random", lambda: 0.99)
        refreshed = client.get("/value")
    assert cache_status(refreshed.headers) == "fwd=stale; stored"
    assert len(calls) == 2


async def test_requests_that_wait_for_another_are_collapsed() -> None:
    FastAPICache.init(InMemoryBackend())
    app, calls = make_app(delay=0.05)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(*(client.get("/value") for _ in range(5)))
    statuses = sorted(cache_status(response.headers) or "" for response in responses)
    assert statuses == ["fwd=miss; collapsed"] * 4 + ["fwd=miss; stored"]
    assert len(calls) == 1


def test_after_the_endpoint_s_own_caches() -> None:
    FastAPICache.init(InMemoryBackend())
    app, _ = make_app()
    with TestClient(app) as client:
        response = client.get("/inner")
    assert response.headers["Cache-Status"] == "Database; hit, Alcazaba; fwd=miss; stored"
