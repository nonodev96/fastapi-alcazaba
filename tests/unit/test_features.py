# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.testclient import TestClient

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.decorator import _compute_etag, _etag_matches, cache
from fastapi_alcazaba.key_builder import default_key_builder
from tests.cache_status import served


class Item(BaseModel):
    name: str
    price: float


def make_app() -> FastAPI:
    app = FastAPI()

    @app.get("/item")
    @cache(expire=60, stale_while_revalidate=30)
    async def item() -> Item:
        return Item(name="thing", price=1.5)

    @app.get("/json-response")
    @cache(expire=60)
    async def json_response() -> JSONResponse:
        return JSONResponse({"a": 1})

    return app


class TestETag:
    def test_is_deterministic_and_quoted(self) -> None:
        etag = _compute_etag(b"payload")
        assert etag == _compute_etag(b"payload")
        assert etag.startswith('W/"')
        assert etag.endswith('"')
        assert etag != _compute_etag(b"other")

    @pytest.mark.parametrize(
        ("header", "matches"),
        [
            ('W/"abc"', True),
            ('"abc"', True),  # weak comparison
            ('"x", W/"abc"', True),
            ("*", True),
            ('"x", "y"', False),
            (None, False),
            ("", False),
        ],
    )
    def test_if_none_match(self, header: str | None, matches: bool) -> None:
        assert _etag_matches(header, 'W/"abc"') is matches

    def test_not_modified_on_hits_and_misses(self) -> None:
        FastAPICache.init(InMemoryBackend())
        with TestClient(make_app()) as client:
            etag = client.get("/item").headers["ETag"]
            hit = client.get("/item", headers={"If-None-Match": etag})
            assert hit.status_code == 304
            assert served(hit.headers) == "hit"
            FastAPICache.reset()
            FastAPICache.init(InMemoryBackend())
            miss = client.get("/item", headers={"If-None-Match": etag})
            assert miss.status_code == 304
            assert served(miss.headers) == "miss"


def test_hits_return_the_model_and_headers() -> None:
    FastAPICache.init(InMemoryBackend())
    with TestClient(make_app()) as client:
        miss = client.get("/item")
        hit = client.get("/item")
    assert miss.json() == hit.json() == {"name": "thing", "price": 1.5}
    assert served(hit.headers) == "hit"
    assert hit.headers["ETag"] == miss.headers["ETag"]
    assert hit.headers["Cache-Control"].startswith("max-age=")
    assert hit.headers["Cache-Control"].endswith(", stale-while-revalidate=30")


def test_returned_responses_get_the_headers_too() -> None:
    """FastAPI ignores the injected response's headers when the endpoint returns a Response."""
    FastAPICache.init(InMemoryBackend())
    with TestClient(make_app()) as client:
        miss = client.get("/json-response")
        hit = client.get("/json-response")
    assert served(miss.headers) == "miss"
    assert miss.headers["ETag"]
    assert served(hit.headers) == "hit"
    assert hit.json() == {"a": 1}


def test_status_header_can_be_turned_off() -> None:
    FastAPICache.init(InMemoryBackend(), cache_status=None)
    with TestClient(make_app()) as client:
        response = client.get("/item")
    assert "Cache-Status" not in response.headers
    assert response.headers["ETag"]


def test_the_cache_s_name_in_cache_status() -> None:
    FastAPICache.init(InMemoryBackend(), cache_status="Shop-API")
    with TestClient(make_app()) as client:
        miss, hit = client.get("/item"), client.get("/item")
    assert miss.headers["Cache-Status"] == "Shop-API; fwd=miss; stored"
    assert hit.headers["Cache-Status"].startswith("Shop-API; hit; ttl=")


@pytest.mark.parametrize("name", ["", "two words", "1st", 'say"what', "caché"])
def test_the_cache_s_name_is_a_token(name: str) -> None:
    with pytest.raises(ValueError, match="cache_status is a token"):
        FastAPICache.init(InMemoryBackend(), cache_status=name)


def test_openapi_keeps_the_response_model() -> None:
    """The decorator keeps the return annotation, where FastAPI finds the response model."""
    schema = make_app().openapi()
    content = schema["paths"]["/item"]["get"]["responses"]["200"]["content"]
    assert content["application/json"]["schema"] == {"$ref": "#/components/schemas/Item"}


class TestExcludeParams:
    async def test_excluded_params_share_an_entry(self) -> None:
        FastAPICache.init(InMemoryBackend())
        calls: list[tuple[str, str]] = []

        @cache(expire=60, exclude_params=["nonce"])
        async def func(name: str, nonce: str = "") -> str:
            calls.append((name, nonce))
            return name

        assert await func("Jon", nonce="a") == "Jon"
        assert await func("Jon", nonce="b") == "Jon"
        assert await func("Ben", nonce="b") == "Ben"
        assert calls == [("Jon", "a"), ("Ben", "b")]

    async def test_positional_arguments(self) -> None:
        FastAPICache.init(InMemoryBackend())
        calls: list[tuple[int, int]] = []

        @cache(expire=60, exclude_params=["b"])
        async def func(a: int, b: int) -> int:
            calls.append((a, b))
            return a

        assert await func(1, 2) == 1
        assert await func(1, 3) == 1
        assert await func(4, 3) == 4
        assert calls == [(1, 2), (4, 3)]

    def test_unknown_names_fail_when_decorating(self) -> None:
        with pytest.raises(ValueError, match="nonexistent"):

            @cache(exclude_params=["nonexistent"])
            async def func(a: int) -> int:
                return a

    async def test_var_keyword_accepts_any_name(self) -> None:
        FastAPICache.init(InMemoryBackend())

        @cache(expire=60, exclude_params=["nonce"])
        async def func(**kwargs: Any) -> dict[str, Any]:
            return kwargs

        # mypy sees a function whose only parameter is **Any as untyped
        assert await func(name="Jon", nonce="a") == {"name": "Jon", "nonce": "a"}  # type: ignore[no-untyped-call]
        assert await func(name="Jon", nonce="b") == {"name": "Jon", "nonce": "a"}  # type: ignore[no-untyped-call]


def test_methods_with_the_same_name_get_different_keys() -> None:
    """Keys use __qualname__: with __name__, these two would share their entries."""

    class First:
        def handler(self) -> None: ...

    class Second:
        def handler(self) -> None: ...

    def key(func: Any) -> str:
        return default_key_builder(func, "ns", args=(), kwargs={})

    assert key(First.handler) != key(Second.handler)


async def test_sync_functions_run_in_the_thread_pool() -> None:
    FastAPICache.init(InMemoryBackend())

    @cache(expire=60)
    def blocking(x: int) -> int:
        return x + 1

    assert list(await asyncio.gather(blocking(1), blocking(1))) == [2, 2]


class TestKeyBuilders:
    async def test_async_key_builders(self) -> None:
        # pylint: disable-next=unused-argument  # a key builder's signature
        async def key_builder(func: Any, namespace: str = "", **kwargs: Any) -> str:
            return f"{namespace}:fixed"

        backend = InMemoryBackend()
        FastAPICache.init(backend, key_builder=key_builder)

        @cache(expire=60, namespace="ns", vary_headers=())
        async def value(x: int) -> int:
            return x

        assert await value(1) == 1
        assert await value(2) == 1  # same key
        assert list(backend._store) == [":ns:fixed"]

    async def test_key_builders_must_return_a_string(self) -> None:
        # pylint: disable-next=unused-argument  # a key builder's signature
        def key_builder(func: Any, namespace: str = "", **kwargs: Any) -> str:
            return 42  # type: ignore[return-value]

        FastAPICache.init(InMemoryBackend(), key_builder=key_builder)

        @cache(expire=60)
        async def value() -> int:
            return 1

        with pytest.raises(TypeError, match="not str"):
            await value()


async def test_unencodable_results_are_returned_without_caching() -> None:
    backend = InMemoryBackend()
    FastAPICache.init(backend)

    @cache(expire=60)
    async def value() -> dict[str, bytes]:
        return {"blob": b"\xff"}  # not UTF-8: jsonable_encoder can't turn it into text

    assert await value() == {"blob": b"\xff"}
    assert not backend._store
