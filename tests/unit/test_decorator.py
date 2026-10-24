import datetime
import time
from collections.abc import Generator
from typing import Any

import pytest
from starlette.testclient import TestClient

from examples.in_memory.main import app
from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend


@pytest.fixture(autouse=True)
def _init_cache(request: pytest.FixtureRequest) -> Generator[Any, Any, None]:  # pyright: ignore[reportUnusedFunction]
    options = request.node.get_closest_marker("cache_options")
    FastAPICache.init(InMemoryBackend(), **(options.kwargs if options else {}))
    yield
    FastAPICache.reset()


def test_datetime() -> None:
    with TestClient(app) as client:
        response = client.get("/datetime")
        assert response.headers.get("X-FastAPI-Cache") == "MISS"
        first = datetime.datetime.fromisoformat(response.json()["now"])
        response = client.get("/datetime")
        assert response.headers.get("X-FastAPI-Cache") == "HIT"
        assert datetime.datetime.fromisoformat(response.json()["now"]) == first
        time.sleep(3)
        response = client.get("/datetime")
        assert response.headers.get("X-FastAPI-Cache") == "MISS"
        assert datetime.datetime.fromisoformat(response.json()["now"]) > first


def test_date() -> None:
    """Test path function without request or response arguments."""
    with TestClient(app) as client:
        response = client.get("/date")
        assert response.headers.get("X-FastAPI-Cache") == "MISS"
        assert datetime.date.fromisoformat(response.json()) == datetime.date.today()

        # do it again to test cache
        response = client.get("/date")
        assert response.headers.get("X-FastAPI-Cache") == "HIT"
        assert datetime.date.fromisoformat(response.json()) == datetime.date.today()

        # now test with cache disabled, as that's a separate code path
        FastAPICache.instance().enable = False
        response = client.get("/date")
        assert "X-FastAPI-Cache" not in response.headers
        assert datetime.date.fromisoformat(response.json()) == datetime.date.today()
        FastAPICache.instance().enable = True


def test_sync() -> None:
    """Ensure that sync function support works."""
    with TestClient(app) as client:
        response = client.get("/sync-me")
        assert response.json() == 42


def test_cache_response_obj() -> None:
    with TestClient(app) as client:
        cache_response = client.get("cache_response_obj")
        assert cache_response.json() == {"a": 1}
        get_cache_response = client.get("cache_response_obj")
        assert get_cache_response.json() == {"a": 1}
        assert get_cache_response.headers.get("cache-control")
        assert get_cache_response.headers.get("etag")


def test_kwargs() -> None:
    with TestClient(app) as client:
        name = "Jon"
        response = client.get("/kwargs", params={"name": name})
        assert "X-FastAPI-Cache" not in response.headers
        assert response.json() == {"name": name}


def test_method() -> None:
    with TestClient(app) as client:
        response = client.get("/method")
        assert response.json() == 17


def test_pydantic_model() -> None:
    with TestClient(app) as client:
        r1 = client.get("/pydantic_instance")
        assert r1.headers.get("X-FastAPI-Cache") == "MISS"
        r2 = client.get("/pydantic_instance")
        assert r2.headers.get("X-FastAPI-Cache") == "HIT"
        assert r1.json() == r2.json()


def test_non_get() -> None:
    with TestClient(app) as client:
        response = client.put("/uncached_put")
        assert "X-FastAPI-Cache" not in response.headers
        first = response.json()["value"]
        response = client.put("/uncached_put")
        assert "X-FastAPI-Cache" not in response.headers
        assert response.json() == {"value": first + 1}


def test_alternate_injected_namespace() -> None:
    with TestClient(app) as client:
        response = client.get("/namespaced_injection")
        assert response.headers.get("X-FastAPI-Cache") == "MISS"
        assert response.json() == {
            "__fastapi_alcazaba_request": 42,
            "__fastapi_alcazaba_response": 17,
        }


@pytest.mark.cache_options(respect_cache_control=True)
def test_cache_control() -> None:
    with TestClient(app) as client:
        # The endpoint counts its calls in a global shared with other tests: compare with the
        # first value, so that the test doesn't depend on the order the tests run in.
        first = client.get("/cached_put").json()["value"]

        # HIT
        response = client.get("/cached_put")
        assert response.json() == {"value": first}

        # no-cache
        response = client.get("/cached_put", headers={"Cache-Control": "no-cache"})
        assert response.json() == {"value": first + 1}

        response = client.get("/cached_put")
        assert response.json() == {"value": first + 1}

        # no-store
        response = client.get("/cached_put", headers={"Cache-Control": "no-store"})
        assert response.json() == {"value": first + 2}

        response = client.get("/cached_put")
        assert response.json() == {"value": first + 1}


def test_client_cache_control_is_ignored_by_default() -> None:
    """Otherwise any client could make every request reach the endpoint."""
    with TestClient(app) as client:
        first = client.get("/cached_put").json()
        response = client.get("/cached_put", headers={"Cache-Control": "no-cache"})
        assert response.json() == first
        assert response.headers["X-FastAPI-Cache"] == "HIT"
        response = client.get("/cached_put", headers={"Cache-Control": "no-store"})
        assert response.json() == first


@pytest.mark.cache_options(respect_cache_control=True)
def test_cache_control_directives_are_parsed() -> None:
    """fastapi-cache2 only recognised the exact header value "no-cache"."""
    with TestClient(app) as client:
        first = client.get("/cached_put").json()["value"]
        response = client.get("/cached_put", headers={"Cache-Control": "max-age=0, No-Cache"})
        assert response.json() == {"value": first + 1}
