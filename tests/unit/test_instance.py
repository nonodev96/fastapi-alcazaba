from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from pydantic import BaseModel
from starlette.testclient import TestClient

from fastapi_alcazaba import Cache, FastAPICache, get_cache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.decorator import cache
from fastapi_alcazaba.key_builder import default_key_builder


class Product(BaseModel):
    id: int
    name: str


class TestSeveralCaches:
    async def test_each_instance_has_its_own_backend_and_settings(self) -> None:
        public, private = InMemoryBackend(), InMemoryBackend()
        public_cache = Cache(public, prefix="public", vary_headers=())
        private_cache = Cache(private, prefix="private", encryption_key="e" * 32)

        @public_cache.cached(expire=60)
        async def catalog() -> list[int]:
            return [1, 2]

        @private_cache.cached(expire=60)
        async def profile() -> dict[str, str]:
            return {"name": "Alice"}

        assert await catalog() == [1, 2]
        assert await profile() == {"name": "Alice"}
        assert all(key.startswith("public:") for key in public._store)
        assert all(key.startswith("private:") for key in private._store)
        (stored,) = (entry.data for entry in private._store.values())
        assert stored.startswith(b"fastapi_alcazaba/e1")

    async def test_instances_dont_need_the_global_cache(self) -> None:
        app_cache = Cache(InMemoryBackend())
        calls: list[int] = []

        @app_cache.cached(expire=60)
        async def value() -> int:
            calls.append(1)
            return 1

        await value()
        await value()
        assert calls == [1]
        with pytest.raises(RuntimeError, match="FastAPICache.init"):
            FastAPICache.instance()

    async def test_the_global_cache_must_be_initialised(self) -> None:
        @cache(expire=60)
        async def value() -> int:
            return 1

        with pytest.raises(RuntimeError, match="FastAPICache.init"):
            await value()

    async def test_endpoint_options_pass_through(self) -> None:
        app_cache = Cache(InMemoryBackend())
        app = FastAPI()

        @app.get("/catalog")
        @app_cache.cached(expire=60, vary_headers=(), stale_while_revalidate=30)
        async def catalog() -> list[int]:
            return [1]

        with TestClient(app) as client:
            client.get("/catalog", headers={"Authorization": "a"})
            response = client.get("/catalog", headers={"Authorization": "b"})
        assert response.headers["X-FastAPI-Cache"] == "HIT"
        assert response.headers["Cache-Control"].endswith("stale-while-revalidate=30")


class TestGetOrSet:
    async def test_computes_once(self) -> None:
        app_cache = Cache(InMemoryBackend())
        calls: list[int] = []

        async def load() -> Product:
            calls.append(1)
            return Product(id=1, name="oil")

        first = await app_cache.get_or_set("product:1", load, expire=60)
        second = await app_cache.get_or_set("product:1", load, expire=60)
        assert first == second == Product(id=1, name="oil")
        assert isinstance(second, Product)  # back as the annotated type
        assert calls == [1]

    async def test_keys_never_reach_the_backend_in_clear(self) -> None:
        backend = InMemoryBackend()
        app_cache = Cache(backend, prefix="app")

        async def load() -> int:
            return 1

        await app_cache.get_or_set("user:alice@example.com", load, namespace="users")
        (key,) = backend._store
        assert key.startswith("app:users:")
        assert "alice" not in key

    async def test_tags(self) -> None:
        app_cache = Cache(InMemoryBackend())
        calls: list[int] = []

        async def load() -> int:
            calls.append(1)
            return len(calls)

        await app_cache.get_or_set("k", load, tags=["catalog"])
        assert await app_cache.invalidate_tags("catalog") == 1
        assert await app_cache.get_or_set("k", load, tags=["catalog"]) == 2


class TestDependency:
    def test_installed_cache_is_injected(self) -> None:
        app_cache = Cache(InMemoryBackend())
        app = FastAPI()
        app_cache.install(app)
        calls: list[int] = []

        @app.get("/products/{product_id}")
        @app_cache.cached(expire=60, tags=lambda product_id, **_: [f"product:{product_id}"])
        async def product(product_id: int) -> Product:
            calls.append(product_id)
            return Product(id=product_id, name="oil")

        @app.post("/products/{product_id}")
        async def update(product_id: int, cache: Annotated[Cache, Depends(get_cache)]) -> int:
            assert cache is app_cache
            return await cache.invalidate_tags(f"product:{product_id}")

        with TestClient(app) as client:
            client.get("/products/1")
            client.get("/products/1")
            assert client.post("/products/1").json() == 1
            client.get("/products/1")
        assert calls == [1, 1]

    def test_falls_back_to_the_global_cache(self) -> None:
        FastAPICache.init(InMemoryBackend())
        app = FastAPI()

        @app.get("/which")
        async def which(cache: Annotated[Cache, Depends(get_cache)]) -> bool:
            return cache is FastAPICache.instance()

        with TestClient(app) as client:
            assert client.get("/which").json() is True


async def test_close_closes_the_backend() -> None:
    class Closable(InMemoryBackend):
        closed = False

        async def close(self) -> None:
            self.closed = True

    backend = Closable()
    await Cache(backend).close()
    assert backend.closed
    await Cache(InMemoryBackend()).close()  # nothing to close


async def test_the_fastapi_cache2_api_still_works() -> None:
    """Code written for fastapi-cache2 reads the configuration through these class methods."""
    backend = InMemoryBackend()
    assert FastAPICache.get_enable() is True  # before init: defaults
    assert FastAPICache.get_expire() is None
    assert FastAPICache.get_options().vary_headers == ("authorization", "cookie")
    assert not FastAPICache.get_metrics().enabled
    FastAPICache.init(backend, prefix="app", expire=30, cache_status_header="X-Cache")
    FastAPICache.init(InMemoryBackend(), prefix="ignored")  # later calls are ignored
    assert FastAPICache.get_backend() is backend
    assert FastAPICache.get_prefix() == "app"
    assert FastAPICache.get_expire() == 30
    assert FastAPICache.get_coder().__name__ == "JsonCoder"
    assert FastAPICache.get_key_builder() is default_key_builder
    assert FastAPICache.get_cache_status_header() == "X-Cache"
    assert FastAPICache.get_enable() is True
    assert FastAPICache.get_guard().timeout == 1.0
    assert FastAPICache.tag_key("t").startswith("app:tag:{")
    await backend.set("app:ns:1", b"x")
    assert await FastAPICache.clear(namespace="ns") == 1
    assert await FastAPICache.invalidate_tags("none") == 0
