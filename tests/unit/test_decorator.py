import datetime
import time
from collections.abc import Generator
from typing import Any

import pytest
from starlette.testclient import TestClient

from examples.in_memory.main import app
from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from tests.cache_status import cache_status, served


@pytest.fixture(autouse=True)
def _init_cache(request: pytest.FixtureRequest) -> Generator[Any, Any, None]:  # pyright: ignore[reportUnusedFunction]
    options = request.node.get_closest_marker("cache_options")
    FastAPICache.init(InMemoryBackend(), **(options.kwargs if options else {}))
    yield
    FastAPICache.reset()


def test_datetime() -> None:
    with TestClient(app) as client:
        response = client.get("/datetime")
        assert cache_status(response.headers) == "fwd=miss; stored"
        first = datetime.datetime.fromisoformat(response.json()["now"])
        response = client.get("/datetime")
        assert cache_status(response.headers) in ("hit; ttl=2", "hit; ttl=1")
        assert datetime.datetime.fromisoformat(response.json()["now"]) == first
        time.sleep(3)
        response = client.get("/datetime")
        assert served(response.headers) == "miss"
        assert datetime.datetime.fromisoformat(response.json()["now"]) > first


def test_date() -> None:
    """Test path function without request or response arguments."""
    with TestClient(app) as client:
        response = client.get("/date")
        assert served(response.headers) == "miss"
        assert datetime.date.fromisoformat(response.json()) == datetime.date.today()

        # do it again to test cache
        response = client.get("/date")
        assert served(response.headers) == "hit"
        assert datetime.date.fromisoformat(response.json()) == datetime.date.today()

        # now test with cache disabled, as that's a separate code path
        FastAPICache.instance().enable = False
        response = client.get("/date")
        assert cache_status(response.headers) == "fwd=bypass"
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
        assert "Cache-Status" not in response.headers  # the endpoint isn't the cached function
        assert response.json() == {"name": name}


def test_method() -> None:
    with TestClient(app) as client:
        response = client.get("/method")
        assert response.json() == 17


def test_pydantic_model() -> None:
    with TestClient(app) as client:
        r1 = client.get("/pydantic_instance")
        assert served(r1.headers) == "miss"
        r2 = client.get("/pydantic_instance")
        assert served(r2.headers) == "hit"
        assert r1.json() == r2.json()


def test_non_get() -> None:
    with TestClient(app) as client:
        response = client.put("/uncached_put")
        assert cache_status(response.headers) == "fwd=method"
        first = response.json()["value"]
        response = client.put("/uncached_put")
        assert cache_status(response.headers) == "fwd=method"
        assert response.json() == {"value": first + 1}


def test_alternate_injected_namespace() -> None:
    with TestClient(app) as client:
        response = client.get("/namespaced_injection")
        assert served(response.headers) == "miss"
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
        assert cache_status(response.headers) == "fwd=request; stored"

        response = client.get("/cached_put")
        assert response.json() == {"value": first + 1}

        # no-store
        response = client.get("/cached_put", headers={"Cache-Control": "no-store"})
        assert response.json() == {"value": first + 2}
        assert cache_status(response.headers) == "fwd=request"

        response = client.get("/cached_put")
        assert response.json() == {"value": first + 1}


def test_client_cache_control_is_ignored_by_default() -> None:
    """Otherwise any client could make every request reach the endpoint."""
    with TestClient(app) as client:
        first = client.get("/cached_put").json()
        response = client.get("/cached_put", headers={"Cache-Control": "no-cache"})
        assert response.json() == first
        assert served(response.headers) == "hit"
        response = client.get("/cached_put", headers={"Cache-Control": "no-store"})
        assert response.json() == first


@pytest.mark.cache_options(respect_cache_control=True)
def test_cache_control_directives_are_parsed() -> None:
    """Not only the exact header value "no-cache": its directives."""
    with TestClient(app) as client:
        first = client.get("/cached_put").json()["value"]
        response = client.get("/cached_put", headers={"Cache-Control": "max-age=0, No-Cache"})
        assert response.json() == {"value": first + 1}
